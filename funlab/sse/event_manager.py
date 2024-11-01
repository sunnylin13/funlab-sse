import queue
import threading
import json
import signal
import atexit
from datetime import datetime, timedelta
from typing import Dict, Any, Set
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager

class EventNotificationSystem:
    def __init__(self, db_url, max_queue_size=1000, max_client_events=100):
        """
        Args:
            db_url (_type_): 數據庫連接URL
            max_queue_size (int, optional): 全局事件隊列最大長度. Defaults to 1000.
            max_client_events (int, optional): 每個客戶端最大事件緩存數量. Defaults to 100.
        """        
        # 數據庫設置
        self.engine = create_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        # Base.metadata.create_all(self.engine)
        
        # 初始化隊列和其他屬性
        self.global_event_queue = queue.Queue(maxsize=max_queue_size)
        self.user_event_queues = {}
        self.active_user_streams = {}
        self.max_client_events = max_client_events
        self.lock = threading.Lock()
        self.is_shutting_down = False
        
        # 恢復未處理的事件
        self._recover_unprocessed_events()
        
        # 啟動事件分發線程
        self.distributor_thread = self.start_event_distributor()
        
        # 註冊關閉處理程序
        atexit.register(self.shutdown)
        signal.signal(signal.SIGTERM, lambda signo, frame: self.shutdown())
        signal.signal(signal.SIGINT, lambda signo, frame: self.shutdown())
    
    @contextmanager
    def get_db_session(self):
        """數據庫會話上下文管理器"""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def _recover_unprocessed_events(self):
        """從數據庫恢復未處理的事件"""
        with self.get_db_session() as session:
            unprocessed_events = session.query(QueuedEvent)\
                .filter_by(processed=False)\
                .filter(QueuedEvent.expires_at > datetime.utcnow())\
                .order_by(QueuedEvent.priority.desc(), QueuedEvent.created_at.asc())\
                .all()
            
            for event_record in unprocessed_events:
                try:
                    self.global_event_queue.put_nowait(event_record.to_event_dict())
                    event_record.processed = True
                    event_record.processing_attempts += 1
                except queue.Full:
                    break  # 隊列已滿，停止恢復
                
            session.commit()
    
    def _store_event(self, event: Dict[str, Any]):
        """將事件存儲到數據庫"""
        with self.get_db_session() as session:
            queued_event = QueuedEvent(
                event_id=event['id'],
                event_type=event['type'],
                data=event['data'],
                user_id=event.get('user_id'),
                is_global=event.get('is_global', False),
                priority=event.get('priority', 2),
                expires_at=datetime.fromisoformat(event['expires_at']) if event.get('expires_at') else None
            )
            session.add(queued_event)
    
    def shutdown(self):
        """優雅關閉系統"""
        if self.is_shutting_down:
            return
        
        self.is_shutting_down = True
        print("正在關閉事件通知系統...")
        
        # 儲存隊列中的未處理事件
        while not self.global_event_queue.empty():
            try:
                event = self.global_event_queue.get_nowait()
                self._store_event(event)
            except queue.Empty:
                break
        
        print("已保存所有未處理事件")
    
    def start_event_distributor(self):
        """啟動事件分發線程"""
        def distributor():
            while not self.is_shutting_down:
                try:
                    event = self.global_event_queue.get(timeout=1)
                    
                    # 更新事件處理狀態
                    with self.get_db_session() as session:
                        event_record = session.query(QueuedEvent)\
                            .filter_by(event_id=event['id'])\
                            .first()
                        
                        if event_record:
                            event_record.processed = True
                            event_record.processing_attempts += 1
                    
                    # 分發事件
                    if event.get('is_global', False):
                        self._distribute_global_event(event)
                    else:
                        self._distribute_user_specific_event(event)
                    
                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"事件分發錯誤: {e}")
        
        thread = threading.Thread(target=distributor, daemon=True)
        thread.start()
        return thread
    
    def create_event(
        self, 
        event_type: str, 
        data: Dict[Any, Any], 
        user_id: int = None, 
        is_global: bool = False,
        priority: int = 2,
        expires_in: int = 3600
    ):
        """創建新事件"""
        event = {
            'id': datetime.utcnow().isoformat(),
            'type': event_type,
            'data': data,
            'user_id': user_id,
            'is_global': is_global,
            'priority': priority,
            'expires_at': (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        }
        
        # 先儲存到數據庫
        self._store_event(event)
        
        try:
            self.global_event_queue.put_nowait(event)
        except queue.Full:
            # 隊列滿時，移除最舊的事件並重試
            try:
                self.global_event_queue.get_nowait()
                self.global_event_queue.put_nowait(event)
            except:
                pass

