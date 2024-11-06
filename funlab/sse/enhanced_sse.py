# config.py
import enum
from datetime import datetime, timedelta
from typing import Dict, Any, Set, Optional, Tuple
import logging

class EventPriority(enum.IntEnum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4

class EventStatus(enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"

class Config:
    # Queue settings
    MAX_QUEUE_SIZE = 1000
    MAX_CLIENT_EVENTS = 100
    
    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 300  # 5 minutes
    
    # Connection settings
    MAX_CONNECTIONS_PER_USER = 5
    HEARTBEAT_INTERVAL = 30
    
    # Database settings
    SQLALCHEMY_DATABASE_URI = 'sqlite:///events.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

# models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, ForeignKey, Enum as SQLAEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()

class QueuedEvent(Base):
    __tablename__ = 'queued_events'
    
    id = Column(Integer, primary_key=True)
    event_type = Column(String(50), nullable=False, index=True)
    data = Column(JSON, nullable=False)
    user_id = Column(Integer, nullable=True, index=True)
    is_global = Column(Boolean, default=False, index=True)
    priority = Column(Integer, default=EventPriority.NORMAL, index=True)
    status = Column(SQLAEnum(EventStatus), default=EventStatus.PENDING, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, default=0)

# utils.py
class EventValidator:
    @staticmethod
    def validate_event(event_data: dict) -> Tuple[bool, str]:
        required_fields = {'type', 'data'}
        if not all(field in event_data for field in required_fields):
            return False, f"Missing required fields: {required_fields - set(event_data.keys())}"
        
        if 'expires_at' in event_data:
            try:
                expires_at = datetime.fromisoformat(event_data['expires_at'])
                if expires_at < datetime.utcnow():
                    return False, "Event already expired"
            except ValueError:
                return False, "Invalid expires_at format"
        
        if 'priority' in event_data:
            try:
                EventPriority(event_data['priority'])
            except ValueError:
                return False, "Invalid priority value"
        
        return True, ""

class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()
    
    def reset(self):
        with self._lock:
            self.total_events = 0
            self.delivered_events = 0
            self.failed_events = 0
            self.global_events = 0
            self.user_events = 0
            self.delivery_times = []
            self.start_time = datetime.utcnow()
    
    def record_event(self, event_type: str, status: EventStatus, delivery_time: Optional[float] = None):
        with self._lock:
            self.total_events += 1
            
            if status == EventStatus.DELIVERED:
                self.delivered_events += 1
            elif status == EventStatus.FAILED:
                self.failed_events += 1
            
            if event_type == 'global':
                self.global_events += 1
            else:
                self.user_events += 1
            
            if delivery_time is not None:
                self.delivery_times.append(delivery_time)
    
    def get_stats(self) -> dict:
        with self._lock:
            return {
                'total_events': self.total_events,
                'delivered_events': self.delivered_events,
                'failed_events': self.failed_events,
                'global_events': self.global_events,
                'user_events': self.user_events,
                'avg_delivery_time': (sum(self.delivery_times) / len(self.delivery_times)) 
                    if self.delivery_times else 0,
                'uptime': (datetime.utcnow() - self.start_time).total_seconds()
            }

# storage.py
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

class EventStorage:
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url)
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        Base.metadata.create_all(self.engine)
    
    @contextmanager
    def session_scope(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def store_event(self, event_data: dict) -> QueuedEvent:
        with self.session_scope() as session:
            event = QueuedEvent(
                event_type=event_data['type'],
                data=event_data['data'],
                user_id=event_data.get('user_id'),
                is_global=event_data.get('is_global', False),
                priority=event_data.get('priority', EventPriority.NORMAL),
                expires_at=datetime.fromisoformat(event_data['expires_at'])
                    if 'expires_at' in event_data else None
            )
            session.add(event)
            session.commit()
            return event
    
    def mark_delivered(self, event_id: int):
        with self.session_scope() as session:
            event = session.query(QueuedEvent).get(event_id)
            if event:
                event.status = EventStatus.DELIVERED
                event.delivered_at = datetime.utcnow()

# connection.py
import queue
import threading
import time
from collections import defaultdict

class ConnectionManager:
    def __init__(self, max_connections_per_user: int = Config.MAX_CONNECTIONS_PER_USER):
        self.max_connections = max_connections_per_user
        self.user_connections: Dict[int, Set[queue.Queue]] = defaultdict(set)
        self.connection_times: Dict[int, float] = {}
        self._lock = threading.Lock()
    
    def add_connection(self, user_id: int, stream: queue.Queue) -> bool:
        with self._lock:
            if len(self.user_connections[user_id]) >= self.max_connections:
                # Remove oldest connection
                oldest_stream = min(
                    self.user_connections[user_id],
                    key=lambda s: self.connection_times.get(id(s), 0)
                )
                self.remove_connection(user_id, oldest_stream)
            
            self.user_connections[user_id].add(stream)
            self.connection_times[id(stream)] = time.time()
            return True
    
    def remove_connection(self, user_id: int, stream: queue.Queue):
        with self._lock:
            if stream in self.user_connections[user_id]:
                self.user_connections[user_id].remove(stream)
                self.connection_times.pop(id(stream), None)
            
            if not self.user_connections[user_id]:
                del self.user_connections[user_id]
    
    def get_user_streams(self, user_id: int) -> Set[queue.Queue]:
        return self.user_connections.get(user_id, set())
    
    def get_all_streams(self) -> Set[queue.Queue]:
        all_streams = set()
        for streams in self.user_connections.values():
            all_streams.update(streams)
        return all_streams

# event_system.py
class EnhancedEventNotificationSystem:
    def __init__(self, db_url: str):
        self.storage = EventStorage(db_url)
        self.metrics = Metrics()
        self.connection_manager = ConnectionManager()
        
        self.global_event_queue = queue.PriorityQueue(maxsize=Config.MAX_QUEUE_SIZE)
        self.user_event_queues: Dict[int, queue.Queue] = {}
        
        self.is_shutting_down = False
        self._lock = threading.Lock()
        
        # Start system
        self.start_event_distributor()
        self.load_stored_events()
        
        # Register shutdown handlers
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)
    
    def create_event(self, event_type: str, data: Any, user_id: Optional[int] = None,
                    is_global: bool = False, priority: EventPriority = EventPriority.NORMAL,
                    expires_in: int = 3600) -> int:
        event_data = {
            'type': event_type,
            'data': data,
            'user_id': user_id,
            'is_global': is_global,
            'priority': priority,
            'expires_at': (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        }
        
        # Validate event
        is_valid, error = EventValidator.validate_event(event_data)
        if not is_valid:
            raise ValueError(f"Invalid event: {error}")
        
        # Store in database
        stored_event = self.storage.store_event(event_data)
        event_data['id'] = stored_event.id
        
        # Add to queue
        try:
            self.global_event_queue.put_nowait((priority, event_data))
        except queue.Full:
            logging.error("Global event queue is full")
            raise RuntimeError("Event system is overloaded")
        
        return stored_event.id
    
    def start_event_distributor(self):
        def distributor():
            while not self.is_shutting_down:
                try:
                    priority, event = self.global_event_queue.get(timeout=1)
                    
                    # Check expiration
                    if 'expires_at' in event:
                        expires_at = datetime.fromisoformat(event['expires_at'])
                        if expires_at < datetime.utcnow():
                            continue
                    
                    start_time = time.time()
                    
                    if event.get('is_global', False):
                        self._distribute_global_event(event)
                    else:
                        self._distribute_user_specific_event(event)
                    
                    # Record metrics
                    delivery_time = time.time() - start_time
                    self.metrics.record_event(
                        'global' if event.get('is_global') else 'user',
                        EventStatus.DELIVERED,
                        delivery_time
                    )
                    
                    # Mark as delivered
                    self.storage.mark_delivered(event['id'])
                
                except queue.Empty:
                    continue
                except Exception as e:
                    logging.error(f"Error in distributor: {e}")
        
        distributor_thread = threading.Thread(target=distributor, daemon=True)
        distributor_thread.start()
    
    def _distribute_global_event(self, event: dict):
        all_streams = self.connection_manager.get_all_streams()
        for stream in all_streams:
            try:
                stream.put_nowait(event)
            except queue.Full:
                logging.warning(f"Stream full for event {event['id']}")
    
    def _distribute_user_specific_event(self, event: dict):
        user_id = event['user_id']
        user_streams = self.connection_manager.get_user_streams(user_id)
        
        if user_streams:
            delivered = False
            for stream in user_streams:
                try:
                    stream.put_nowait(event)
                    delivered = True
                except queue.Full:
                    continue
            
            if not delivered:
                self._handle_failed_delivery(event, "All user streams full")
        else:
            # Queue for later delivery
            if user_id not in self.user_event_queues:
                self.user_event_queues[user_id] = queue.Queue(
                    maxsize=Config.MAX_CLIENT_EVENTS
                )
            try:
                self.user_event_queues[user_id].put_nowait(event)
            except queue.Full:
                self._handle_failed_delivery(event, "User queue full")
    
    def _handle_failed_delivery(self, event: dict, error: str):
        try:
            with self.storage.session_scope() as session:
                queued_event = session.query(QueuedEvent).get(event['id'])
                if queued_event and queued_event.retry_count < Config.MAX_RETRIES:
                    queued_event.retry_count += 1
                    queued_event.status = EventStatus.PENDING
                    # Schedule retry
                    threading.Timer(
                        Config.RETRY_DELAY,
                        lambda: self.global_event_queue.put(
                            (event.get('priority', EventPriority.NORMAL), event)
                        )
                    ).start()
                else:
                    queued_event.status = EventStatus.FAILED
                    logging.error(f"Event {event['id']} failed: {error}")
        except Exception as e:
            logging.error(f"Error handling failed delivery: {e}")

# app.py
from flask import Flask, Response, jsonify, request
from flask_login import login_required, current_user

def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    
    event_system = EnhancedEventNotificationSystem(app.config['SQLALCHEMY_DATABASE_URI'])
    
    @app.route('/events/stream')
    @login_required
    def event_stream():
        user_stream = queue.Queue(maxsize=Config.MAX_CLIENT_EVENTS)
        user_id = current_user.id
        
        event_system.connection_manager.add_connection(user_id, user_stream)
        
        def generate():
            try:
                while not event_system.is_shutting_down:
                    try:
                        event = user_stream.get(timeout=Config.HEARTBEAT_INTERVAL)
                        
                        # Check expiration
                        if event.get('expires_at'):
                            expires_at = datetime.fromisoformat(event['expires_at'])
                            if expires_at < datetime.utcnow():
                                continue
                        
                        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield "event: heartbeat\ndata: keep-alive\n\n"
            finally:
                event_system.connection_manager.remove_connection(user_id, user_stream)
        
        return Response(
            generate(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    
    @app.route('/metrics')
    @login_required
    def get_metrics():
        return json