from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import asyncio
import uuid
from datetime import datetime
from pathlib import Path
import logging
from typing import List, Dict, Any, Optional
import sys
import json
import re
import aiohttp
import pytz
from urllib.parse import unquote
import psycopg
from psycopg_pool import AsyncConnectionPool
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

# 큐 시스템 추가 imports
import asyncio
from asyncio import Queue, Event
from dataclasses import dataclass
from typing import Callable
import time

# 큐 작업 데이터 클래스
@dataclass
class QueueTask:
    task_id: str
    user_id: str
    username: str
    image_urls: list
    data: dict
    result_future: asyncio.Future
    created_at: float

# 큐 시스템 설정
DB_WRITE_QUEUE = Queue(maxsize=10000)  # 최대 10,000개 작업 대기
QUEUE_WORKERS = 10  # DB 쓰기 워커 수
BATCH_SIZE = 10    # 배치 처리 크기
BATCH_TIMEOUT = 0.5  # 배치 대기 시간 (초)

# 큐 워커 상태 관리
queue_workers_running = False
worker_tasks = []

async def start_queue_workers():
    """큐 워커들 시작"""
    global queue_workers_running, worker_tasks
    if queue_workers_running:
        return
    
    queue_workers_running = True
    worker_tasks = []
    
    for i in range(QUEUE_WORKERS):
        task = asyncio.create_task(db_queue_worker(f"worker-{i}"))
        worker_tasks.append(task)
    
    logger.info(f"큐 워커 {QUEUE_WORKERS}개 시작됨")

async def stop_queue_workers():
    """큐 워커들 종료"""
    global queue_workers_running, worker_tasks
    queue_workers_running = False
    
    # 모든 워커 작업 취소
    for task in worker_tasks:
        task.cancel()
    
    # 워커들이 완전히 종료될 때까지 대기
    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)
    
    worker_tasks = []
    logger.info("모든 큐 워커 종료됨")

async def db_queue_worker(worker_name: str):
    """DB 쓰기 전용 큐 워커"""
    logger.info(f"큐 워커 {worker_name} 시작")
    
    while queue_workers_running:
        try:
            # 배치로 작업들 수집
            batch = []
            batch_start_time = time.time()
            
            # 첫 번째 작업 대기 (블로킹)
            try:
                first_task = await asyncio.wait_for(
                    DB_WRITE_QUEUE.get(), 
                    timeout=5.0
                )
                batch.append(first_task)
            except asyncio.TimeoutError:
                continue  # 타임아웃되면 다시 시도
            
            # 추가 작업들 수집 (배치 크기나 시간 제한까지)
            while (len(batch) < BATCH_SIZE and 
                   time.time() - batch_start_time < BATCH_TIMEOUT):
                try:
                    task = await asyncio.wait_for(
                        DB_WRITE_QUEUE.get(), 
                        timeout=0.1
                    )
                    batch.append(task)
                except asyncio.TimeoutError:
                    break  # 더 이상 작업이 없으면 배치 처리 진행
            
            # 배치 처리 실행
            if batch:
                await process_batch(worker_name, batch)
                
        except asyncio.CancelledError:
            logger.info(f"큐 워커 {worker_name} 취소됨")
            break
        except Exception as e:
            logger.error(f"큐 워커 {worker_name} 에러: {str(e)}")
            await asyncio.sleep(1)  # 에러 발생 시 잠시 대기
    
    logger.info(f"큐 워커 {worker_name} 종료")

async def process_batch(worker_name: str, batch: list):
    """배치 단위로 DB 작업 처리"""
    logger.info(f"{worker_name}: 배치 처리 시작 ({len(batch)}개 작업)")
    
    for task in batch:
        try:
            # 개별 작업 처리
            result = await process_single_task(task)
            
            # 결과를 Future에 설정
            if not task.result_future.done():
                task.result_future.set_result(result)
                
        except Exception as e:
            # 에러를 Future에 설정
            if not task.result_future.done():
                task.result_future.set_exception(e)
            logger.error(f"{worker_name}: 작업 처리 실패 - {task.task_id}: {str(e)}")
        finally:
            # 큐에서 작업 완료 표시
            DB_WRITE_QUEUE.task_done()
    
    logger.info(f"{worker_name}: 배치 처리 완료 ({len(batch)}개 작업)")

# 큐 워커에서 에러 처리 강화
async def process_single_task(task: QueueTask) -> dict:
    """개별 작업 처리 (실제 DB 저장)"""
    start_time = time.time()
    try:
        logger.info(f"작업 시작: {task.task_id} (대기시간: {start_time - task.created_at:.2f}초)")
        
        # 이미지 다운로드
        downloaded_images = []
        saved_files = []
        
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            download_tasks = [
                download_kakao_image(session, url, task.user_id, task.username) 
                for url in task.image_urls
            ]
            
            if download_tasks:
                results = await asyncio.gather(*download_tasks, return_exceptions=True)
                
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        logger.error(f"이미지 다운로드 실패 {i+1}: {str(result)}")
                        downloaded_images.append({
                            "status": "error",
                            "error": str(result)[:100],
                            "url": task.image_urls[i] if i < len(task.image_urls) else "unknown"
                        })
                    else:
                        downloaded_images.append(result)
                        if result.get("status") == "success":
                            saved_files.append(result.get("file_path"))
        
        # 성공한 다운로드 수 계산
        success_count = sum(1 for img in downloaded_images if img.get("status") == "success")
        
        # 모든 이미지가 성공하지 않으면 롤백
        if task.image_urls and success_count != len(task.image_urls):
            await cleanup_files(saved_files)
            logger.warning(f"작업 실패: {task.task_id} - 다운로드 {success_count}/{len(task.image_urls)}")
            return {
                "success": False,
                "error": "이미지 다운로드 실패",
                "success_count": success_count,
                "total_count": len(task.image_urls),
                "saved_to_db_count": 0
            }
        
        # DB 저장 (재시도 로직 간소화)
        saved_to_db_count = 0
        if success_count > 0:
            try:
                async with db_pool.connection() as conn:
                    async with conn.transaction():
                        for i, img in enumerate(downloaded_images):
                            if img.get("status") == "success":
                                db_saved = await save_image_upload_to_db_in_transaction(
                                    conn=conn,
                                    username=task.username,
                                    original_url=task.image_urls[i],
                                    user_id=task.user_id,
                                    image_data=img
                                )
                                
                                if not db_saved:
                                    raise Exception(f"DB 저장 실패: 이미지 {i+1}")
                        
                        saved_to_db_count = success_count
                        
            except Exception as db_error:
                await cleanup_files(saved_files)
                return {
                    "success": False,
                    "error": f"DB 저장 실패",
                    "success_count": 0,
                    "total_count": len(task.image_urls),
                    "saved_to_db_count": 0
                }
        
        processing_time = time.time() - start_time
        
        return {
            "success": True,
            "success_count": success_count,
            "total_count": len(task.image_urls),
            "saved_to_db_count": saved_to_db_count,
            "processing_time": processing_time
        }
        
    except Exception as e:
        await cleanup_files(saved_files)
        return {
            "success": False,
            "error": str(e)[:100],  # 에러 메시지 길이 제한
            "success_count": 0,
            "total_count": len(task.image_urls) if task.image_urls else 0,
            "saved_to_db_count": 0
        }

async def submit_to_queue(user_id: str, username: str, image_urls: list, data: dict) -> dict:
    """작업을 큐에 제출하고 결과 대기"""
    # 큐가 가득 찬 경우 처리
    if DB_WRITE_QUEUE.qsize() >= DB_WRITE_QUEUE.maxsize * 0.8:
        logger.warning(f"큐가 거의 가득참: {DB_WRITE_QUEUE.qsize()}/{DB_WRITE_QUEUE.maxsize}")
        return {
            "success": False,
            "error": "서버가 과부하 상태입니다. 잠시 후 다시 시도해주세요."
        }
    
    # 빈 이미지 URL 체크
    if not image_urls:
        return {
            "success": False,
            "error": "이미지 URL 없음"
        }
    
    # 작업 생성
    task_id = f"{user_id[:8]}_{int(time.time() * 1000)}"
    result_future = asyncio.Future()
    
    task = QueueTask(
        task_id=task_id,
        user_id=user_id,
        username=username,
        image_urls=image_urls,
        data=data,
        result_future=result_future,
        created_at=time.time()
    )
    
    # 큐에 작업 제출
    try:
        await asyncio.wait_for(
            DB_WRITE_QUEUE.put(task), 
            timeout=5.0
        )
        logger.info(f"작업 큐에 제출됨: {task_id} (큐 크기: {DB_WRITE_QUEUE.qsize()})")
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": "큐 제출 타임아웃"
        }
    
    # 결과 대기 (최대 60초)
    try:
        result = await asyncio.wait_for(result_future, timeout=60.0)
        logger.info(f"작업 완료: {task_id}")
        return result
    except asyncio.TimeoutError:
        logger.error(f"작업 타임아웃: {task_id}")
        return {
            "success": False,
            "error": "작업 처리 타임아웃"
        }


# 한국 시간 유틸리티 함수들
def get_kst_time() -> str:
    """한국 시간 반환 (문자열)"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst).strftime('%Y-%m-%d %H:%M:%S')

def get_kst_date() -> str:
    """한국 날짜 반환 (문자열)"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst).strftime('%Y.%m.%d')

def get_kst_timestamp() -> str:
    """한국 시간 타임스탬프 반환 (파일명용)"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst).strftime('%Y%m%d_%H%M%S')

def get_kst_datetime() -> datetime:
    """한국 시간 datetime 객체 반환"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)

def get_kst_date_folder() -> str:
    """한국 시간 기준 날짜 폴더명 반환 (YYMMDD 형식)"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst).strftime('%y%m%d')

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',  # 더 간단한 포맷
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler('server.log', encoding='utf-8', maxBytes=50*1024*1024, backupCount=3)  # 로그 로테이션 추가
    ]
)
logger = logging.getLogger(__name__)

# FastAPI 앱 생성
app = FastAPI(
    title="Kakao Image Upload API",
    description="카카오톡 이미지 업로드 및 처리 서버",
    version="2.0.0"
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 설정 상수
KAKAO_IMAGE_DIR = Path("/Authfiles/kakao_images")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
MAX_CONCURRENT_UPLOADS = 2000

# PostgreSQL 설정
DATABASE_CONFIG = {
    "host": "dpg-d37aglogjchc73c45dh0-a.oregon-postgres.render.com",
    "database": "chatbot_ain6",
    "port": 5432,
    "user": "chatbot_ain6_user",
    "password": "QLC4mbPSwJuZR0LVvKZFhnjC80bCjacj"
}

# 전역 변수
db_pool: Optional[AsyncConnectionPool] = None
image_counter: Dict[str, int] = {}  # 동일 사용자의 이미지 카운터

# 디렉토리 생성
KAKAO_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
logger.info(f"카카오 이미지 디렉토리 확인: {KAKAO_IMAGE_DIR.absolute()}")

# 동시 업로드 제한
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
logger.info(f"동시 업로드 제한: {MAX_CONCURRENT_UPLOADS}개")

async def init_database():
    """데이터베이스 연결 풀 초기화"""
    global db_pool
    try:
        # PostgreSQL 연결 문자열 생성
        connection_string = f"postgresql://{DATABASE_CONFIG['user']}:{DATABASE_CONFIG['password']}@{DATABASE_CONFIG['host']}:{DATABASE_CONFIG['port']}/{DATABASE_CONFIG['database']}"
        
        db_pool = AsyncConnectionPool(
            connection_string,
            min_size=5,
            max_size=60,
            timeout=30
        )
        logger.info("PostgreSQL 연결 풀 생성 완료")
        
    except Exception as e:
        logger.error(f"데이터베이스 연결 실패: {str(e)}")
        raise

async def close_database():
    """데이터베이스 연결 종료"""
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("PostgreSQL 연결 풀 종료 완료")

def validate_kakao_request(data: Dict[Any, Any]) -> bool:
    """카카오톡 요청 데이터 유효성 검사"""
    required_fields = [
        ("intent", ["id", "name"]),
        ("userRequest", ["timezone", "block", "utterance", "user"]),
        ("bot", ["id", "name"]),
        ("action", ["name", "id"])
    ]
    
    try:
        for field, sub_fields in required_fields:
            if field not in data:
                return False
            
            for sub_field in sub_fields:
                if sub_field not in data[field]:
                    return False
        
        # 중요한 중첩 필드들 검사
        user_request = data["userRequest"]
        if "id" not in user_request["user"] or "type" not in user_request["user"]:
            return False
        if "id" not in user_request["block"] or "name" not in user_request["block"]:
            return False
            
        return True
    except (KeyError, TypeError):
        return False

def generate_unique_filename(username: str, user_id: str, extension: str = ".jpg") -> tuple[str, Path]:
    """
    고유한 파일명 생성 및 serial_number별 폴더 경로 반환
    Returns: (filename, full_directory_path)
    """
    global image_counter
    
    # serial_number는 user_id의 앞 8자리
    serial_number = user_id[:8]
    
    # 날짜별 폴더 생성 (YYMMDD 형식)
    date_folder = get_kst_date_folder()
    
    # serial_number별 폴더 경로: /Authfiles/kakao_images/날짜/serial_number
    serial_dir = KAKAO_IMAGE_DIR / date_folder / serial_number
    serial_dir.mkdir(parents=True, exist_ok=True)
    
    # 사용자별 카운터 키 생성
    counter_key = f"{serial_number}_{date_folder}"
    
    # 카운터 증가
    if counter_key not in image_counter:
        # 해당 폴더의 기존 파일들 확인하여 카운터 초기화
        existing_files = list(serial_dir.glob(f"{serial_number}_*{extension}"))
        if existing_files:
            # 가장 큰 인덱스 번호 찾기
            max_idx = 0
            for file in existing_files:
                try:
                    # 파일명에서 인덱스 추출 (serial_number_timestamp_idx.확장자)
                    parts = file.stem.split('_')
                    if len(parts) >= 3:
                        idx = int(parts[-1])  # 마지막 부분이 인덱스
                        max_idx = max(max_idx, idx)
                except (ValueError, IndexError):
                    continue
            image_counter[counter_key] = max_idx + 1
        else:
            image_counter[counter_key] = 1
    else:
        image_counter[counter_key] += 1
    
    # 타임스탬프 생성
    timestamp = get_kst_timestamp()
    
    # 파일명 생성: serial_number_timestamp_idx.확장자
    filename = f"{serial_number}_{timestamp}_{image_counter[counter_key]}{extension}"
    
    return filename, serial_dir

def extract_image_urls_from_kakao_data(data: Dict[Any, Any]) -> List[str]:
    """카카오톡 데이터에서 이미지 URL들 추출"""
    urls = []
    
    try:
        # action > detailParams에서 userimage 데이터 추출
        detail_params = data.get("action", {}).get("detailParams", {})
        userimage_data = detail_params.get("userimage", {})
        
        # userimage의 value가 JSON 문자열인 경우
        userimage_value = userimage_data.get("value", "")
        if userimage_value:
            try:
                # JSON 파싱
                parsed_data = json.loads(userimage_value)
                secure_urls_str = parsed_data.get("secureUrls", "")
                
                if secure_urls_str:
                    # "List(...)" 형태에서 URL 추출
                    if secure_urls_str.startswith("List(") and secure_urls_str.endswith(")"):
                        urls_content = secure_urls_str[5:-1]  # "List(" 와 ")" 제거
                        url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
                        found_urls = re.findall(url_pattern, urls_content)
                        urls.extend(found_urls)
                    else:
                        # 직접 URL 문자열인 경우
                        url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
                        found_urls = re.findall(url_pattern, secure_urls_str)
                        urls.extend(found_urls)
                        
            except json.JSONDecodeError:
                # JSON 파싱 실패시 문자열에서 직접 URL 추출
                url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
                found_urls = re.findall(url_pattern, userimage_value)
                urls.extend(found_urls)
        
        # userimage의 origin에서도 확인 (백업용)
        userimage_origin = userimage_data.get("origin", "")
        if userimage_origin and "http" in userimage_origin:
            url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
            found_urls = re.findall(url_pattern, userimage_origin)
            urls.extend(found_urls)
        
        # params에서도 확인 (백업용)
        params = data.get("action", {}).get("params", {})
        userimage_param = params.get("userimage", "")
        if userimage_param and "http" in userimage_param:
            try:
                parsed_data = json.loads(userimage_param)
                secure_urls_str = parsed_data.get("secureUrls", "")
                if secure_urls_str:
                    url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
                    found_urls = re.findall(url_pattern, secure_urls_str)
                    urls.extend(found_urls)
            except (json.JSONDecodeError, TypeError):
                url_pattern = r'https?://[^\s,)"\'\]]+(?:\?[^\s,)"\'\]]+)?'
                found_urls = re.findall(url_pattern, userimage_param)
                urls.extend(found_urls)
        
        # 중복 제거 및 빈 URL 제거
        urls = list(set([url for url in urls if url and len(url) > 10]))
        
    except Exception as e:
        logger.error(f"URL 추출 실패: {str(e)}")
    
    return urls

async def download_kakao_image(session: aiohttp.ClientSession, url: str, user_id: str, username: str) -> Dict[str, Any]:
    """카카오톡 이미지 다운로드 및 저장"""
    try:
        logger.info(f"이미지 다운로드 시작 - 사용자: {username} ({user_id})")
        logger.info(f"다운로드 URL: {url}")
        
        # 이미지 다운로드
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                logger.error(f"이미지 다운로드 실패: HTTP {response.status}")
                return {"status": "error", "error": f"HTTP {response.status}"}
            
            # Content-Type 확인
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                logger.error(f"이미지가 아닌 파일: {content_type}")
                return {"status": "error", "error": f"Invalid content type: {content_type}"}
            
            # 이미지 데이터 읽기
            image_data = await response.read()
            
            if len(image_data) == 0:
                logger.error("빈 이미지 파일")
                return {"status": "error", "error": "Empty image file"}
            
            if len(image_data) > MAX_FILE_SIZE:
                logger.error(f"파일 크기 초과: {len(image_data):,} bytes")
                return {"status": "error", "error": f"File too large: {len(image_data):,} bytes"}
        
        # 확장자 결정 (Content-Type 기반)
        extension = ".jpg"  # 기본값
        if "png" in content_type:
            extension = ".png"
        elif "gif" in content_type:
            extension = ".gif"
        elif "webp" in content_type:
            extension = ".webp"
        
        # 파일명 및 경로 생성
        filename, date_dir = generate_unique_filename(username, user_id, extension)
        file_path = date_dir / filename
        
        # 파일 저장
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(image_data)
        
        logger.info(f"이미지 저장 완료: {file_path} ({len(image_data):,} bytes)")
        
        return {
            "status": "success",
            "filename": filename,
            "file_path": str(file_path),
            "file_size": len(image_data),
            "content_type": content_type,
            "original_url": url,
            "date_folder": date_dir.name
        }
        
    except asyncio.TimeoutError:
        logger.error("이미지 다운로드 타임아웃")
        return {"status": "error", "error": "Download timeout"}
    except Exception as e:
        logger.error(f"이미지 다운로드 실패: {str(e)}")
        return {"status": "error", "error": str(e)}
    
def format_request_summary_from_result(data: Dict[Any, Any], result: dict) -> str:
    """요청 정보를 요약 형태로 포맷팅"""
    user_request = data["userRequest"]
    user_id = user_request["user"]["id"]
    serial_number = user_id[:8]

    summary = f"""보내주신 인증서({result['total_count']}장)은 정상적으로 접수되었습니다.📩({get_kst_date()})

🔈고유번호는 [{serial_number}]입니다. 
(2025.09.22부터 신 고유번호 배정중)

🐰최립우 연습생🐰을 위한 소중한 투표 감사드립니다.🥰

✔️9월 19, 20, 21일에 배정됐던 구 고유번호(알파벳대문자2+숫자3) 투표도 정상적으로 집계될 예정이니, 걱정하지 않으셔도 됩니다.
✔️또한 당첨자 발표 후 순차적으로 개별 안내가 발송됩니다.

✔️이벤트 관련 안내는 공지사항을 통해 업데이트 되니, 많은 관심 부탁드립니다.""" 
    return summary

@app.on_event("startup")
async def startup_event():
    """서버 시작 시 실행되는 이벤트"""
    logger.info("=" * 60)
    logger.info("카카오톡 이미지 업로드 API 서버 시작")
    logger.info("=" * 60)
    logger.info(f"이미지 저장 디렉토리: {KAKAO_IMAGE_DIR.absolute()}")
    logger.info(f"최대 파일 크기: {MAX_FILE_SIZE // (1024*1024)}MB")
    logger.info(f"지원 파일 형식: {', '.join(ALLOWED_EXTENSIONS)}")
    logger.info(f"최대 동시 업로드: {MAX_CONCURRENT_UPLOADS}개")
    
    # 데이터베이스 초기화
    await init_database()

    # 큐 워커 시작
    await start_queue_workers()
    
    logger.info(f"큐 설정: 워커 {QUEUE_WORKERS}개, 최대 큐 크기 {DB_WRITE_QUEUE.maxsize}, 배치 크기 {BATCH_SIZE}")
    logger.info("=" * 60)

@app.on_event("shutdown")
async def shutdown_event():
    """서버 종료 시 실행되는 이벤트"""
    logger.info("서버 종료 중...")

    # 큐 워커 종료
    await stop_queue_workers()
    # 데이터베이스 연결 종료
    await close_database()
    
    logger.info("안전하게 종료되었습니다.")

# 큐 상태 모니터링 개선
@app.get("/queue/status")
async def queue_status():
    """큐 상태 확인"""
    queue_size = DB_WRITE_QUEUE.qsize()
    queue_usage = (queue_size / DB_WRITE_QUEUE.maxsize) * 100
    
    return JSONResponse({
        "queue_size": queue_size,
        "max_queue_size": DB_WRITE_QUEUE.maxsize,
        "queue_usage_percent": round(queue_usage, 1),
        "status": "high" if queue_usage > 80 else "medium" if queue_usage > 50 else "normal",
        "workers_running": queue_workers_running,
        "active_workers": len(worker_tasks),
        "batch_size": BATCH_SIZE,
        "batch_timeout": BATCH_TIMEOUT,
        "db_pool_size": db_pool.get_stats() if db_pool else None
    })

@app.post("/kakao/chat")
async def process_kakao_request(request: Request):
    """카카오톡 챗봇 요청 처리 및 이미지 저장 (트랜잭션 방식)"""
    try:
        # JSON 데이터 받기
        data = await request.json()
        
        # 요청 전체를 로그에 출력 (개발/디버깅용)
        # logger.info("="*80)
        # logger.info("카카오톡 요청 데이터:")
        # logger.info(f"{json.dumps(data, indent=2, ensure_ascii=False)}")
        # logger.info("="*80)
        
        # 데이터 유효성 검사
        if not validate_kakao_request(data):
            logger.warning("잘못된 카카오톡 요청 형식")
            raise HTTPException(status_code=400, detail="잘못된 요청 형식입니다.")
        
        # 요청 데이터 파싱
        user_request = data["userRequest"]
        user_id = user_request["user"]["id"]
                
        # 이미지 URL 추출 및 다운로드
        image_urls = extract_image_urls_from_kakao_data(data)
        
        if not image_urls:
            logger.warning(f"이미지 없음: {user_id[:8]}")
            return {
                "version": "2.0",
                "template": {
                    "outputs": [{
                        "simpleText": {
                            "text": "이미지가 감지되지 않았습니다. 다시 업로드해주세요."
                        }
                    }]
                }
            }
        
        # 🔥 큐 시스템으로 작업 제출
        result = await submit_to_queue(user_id, '', image_urls, data)
        
        if not result["success"]:
            # 실패 응답
            return {
                "version": "2.0",
                "template": {
                    "outputs": [{
                        "simpleText": {
                            "text": f"❌ 접속 증가로 오류가 발생하였습니다.\n 잠시후 다시 인증서를 업로드 해주세요."
                        }
                    }]
                }
            }
        
        # 응답 텍스트 생성
        response_text = format_request_summary_from_result(data, result)
        
        logger.info(f"카카오톡 요청 처리 완료 - 사용자: {user_id}, 이미지: {result['success_count']}/{result['total_count']}개, DB저장: {result['saved_to_db_count']}개")
        
        # 카카오톡 표준 응답 형식으로 반환
        return {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "simpleText": {
                            "text": response_text
                        }
                    }
                ]
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"카카오톡 요청 처리 실패: {str(e)}")
        return {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "simpleText": {
                            "text": "죄송합니다. 요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
                        }
                    }
                ]
            }
        }

async def save_image_upload_to_db_in_transaction(
    conn,  # psycopg connection object
    username: str,
    original_url: str, 
    user_id: str,
    image_data: Dict[str, Any]
) -> bool:
    """트랜잭션 내에서 이미지 업로드 정보를 데이터베이스에 저장"""
    
    serial_number = user_id[:8]

    insert_sql = """
    INSERT INTO kakao_image_uploads (
        username, serial_number, user_id, original_url, filename, file_path, 
        file_size, content_type, upload_time
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    try:
        await conn.execute(
            insert_sql,
            (
                username,
                serial_number,
                user_id,
                original_url,
                image_data["filename"],
                image_data["file_path"],
                image_data["file_size"],
                image_data["content_type"],
                get_kst_time()
            )
        )
        logger.info(f"DB 저장 완료 (트랜잭션): {image_data['filename']}")
        return True
    except Exception as e:
        logger.error(f"DB 저장 실패 (트랜잭션): {str(e)}")
        return False
    
async def cleanup_files(file_paths: list):
    """파일 정리 함수 (롤백용)"""
    for file_path in file_paths:
        try:
            if file_path and Path(file_path).exists():
                Path(file_path).unlink()
                logger.info(f"롤백: 파일 삭제 - {file_path}")
        except Exception as e:
            logger.error(f"파일 삭제 실패 - {file_path}: {str(e)}")

@app.get("/health")
async def health_check():
    """헬스체크"""
    # DB 연결 상태 확인
    db_status = "connected" if db_pool else "disconnected"
    
    return JSONResponse({
        "status": "healthy",
        "timestamp": get_kst_datetime().isoformat(),
        "current_date_folder": get_kst_date_folder(),
        "kakao_image_dir": str(KAKAO_IMAGE_DIR),
        "max_file_size_mb": MAX_FILE_SIZE // (1024*1024),
        "allowed_extensions": list(ALLOWED_EXTENSIONS),
        "database_status": db_status
    })

@app.get("/")
async def root():
    """루트 경로"""
    return JSONResponse({
        "message": "카카오톡 이미지 업로드 API 서버",
        "version": "2.0.0",
        "current_date_folder": get_kst_date_folder(),
        "endpoints": {
            "kakao_chat": "/kakao/chat",
            "health_check": "/health",
            "api_docs": "/docs"
        }
    })

if __name__ == "__main__":
    import uvicorn
    
    print("=" * 70)
    print("카카오톡 이미지 업로드 API 서버 시작")
    print("=" * 70)
    print(f"이미지 저장 디렉토리: {KAKAO_IMAGE_DIR.absolute()}")
    print(f"오늘 날짜 폴더: {get_kst_date_folder()}")
    print(f"최대 파일 크기: {MAX_FILE_SIZE // (1024*1024)}MB")
    print(f"지원 파일 형식: {', '.join(ALLOWED_EXTENSIONS)}")
    print(f"최대 동시 업로드: {MAX_CONCURRENT_UPLOADS}개")
    print("=" * 70)
    print("서버 주소:")
    print("   - 메인: http://localhost:8000")
    print("   - API 문서: http://localhost:8000/docs")
    print("   - 헬스체크: http://localhost:8000/health")
    print("=" * 70)
    print("주요 엔드포인트:")
    print("   - POST /kakao/chat : 카카오톡 챗봇 응답")
    print("   - GET  /health     : 헬스체크")
    print("=" * 70)
    print("서버를 중지하려면 Ctrl+C를 누르세요")
    print("=" * 70)
    
    try:
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8000,
            workers=17,
            reload=True,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n" + "=" * 70)
        print("서버가 안전하게 종료되었습니다.")
        print("=" * 70)