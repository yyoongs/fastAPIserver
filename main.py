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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('server.log', encoding='utf-8')
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
            min_size=1,
            max_size=10,
            timeout=60
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

async def save_image_upload_to_db(
    username: str,
    original_url: str, 
    user_id: str,
    image_data: Dict[str, Any]
) -> bool:
    """이미지 업로드 정보를 데이터베이스에 저장"""
    global db_pool
    if not db_pool:
        logger.error("데이터베이스 연결 풀이 초기화되지 않음")
        return False
    
    serial_number = user_id[:8]

    insert_sql = """
    INSERT INTO kakao_image_uploads (
        username, serial_number, user_id, original_url, filename, file_path, 
        file_size, content_type, upload_time
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    try:
        async with db_pool.connection() as conn:
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
        logger.info(f"DB 저장 완료: {image_data['filename']}")
        return True
    except Exception as e:
        logger.error(f"DB 저장 실패: {str(e)}")
        return False
    
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

def format_request_summary(data: Dict[Any, Any], success_count: int, total_images: int, date_folder: str = None) -> str:
    """요청 정보를 요약 형태로 포맷팅"""
    user_request = data["userRequest"]
    user_id = user_request["user"]["id"]
    serial_number = user_id[:8]

    summary = f"""보내주신 인증서({total_images}장)은 정상적으로 접수되었습니다. ({get_kst_date()})
고유번호는 [{serial_number}]입니다. 
(2025.09.22부터 신 고유번호 배정중)

최립우 연습생을 위한 소중한 투표 감사드립니다.

9월 19, 20, 21일에 배정됐던 구 고유번호(알파벳대문자2+숫자3) 투표도 정상적으로 집계될 예정이니, 걱정하지 않으셔도 됩니다.
또한 당첨자 발표 후 순차적으로 개별 안내가 발송됩니다.

이벤트 관련 안내는 공지사항을 통해 업데이트 되니, 많은 관심 부탁드립니다.""" 
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
    
    logger.info("=" * 60)

@app.on_event("shutdown")
async def shutdown_event():
    """서버 종료 시 실행되는 이벤트"""
    logger.info("서버 종료 중...")
    
    # 데이터베이스 연결 종료
    await close_database()
    
    logger.info("안전하게 종료되었습니다.")

@app.post("/kakao/chat")
async def process_kakao_request(request: Request):
    """카카오톡 챗봇 요청 처리 및 이미지 저장"""
    try:
        # JSON 데이터 받기
        data = await request.json()
        
        # 요청 전체를 로그에 출력 (개발/디버깅용)
        logger.info("="*80)
        logger.info("카카오톡 요청 데이터:")
        logger.info(f"{json.dumps(data, indent=2, ensure_ascii=False)}")
        logger.info("="*80)
        
        # 데이터 유효성 검사
        if not validate_kakao_request(data):
            logger.warning("잘못된 카카오톡 요청 형식")
            raise HTTPException(status_code=400, detail="잘못된 요청 형식입니다.")
        
        # 요청 데이터 파싱
        user_request = data["userRequest"]
        user_message = user_request["utterance"]
        user_id = user_request["user"]["id"]

        # action params에서 username 추출
        action_params = data.get("action", {}).get("params", {})
        username = action_params.get("username", "Unknown")
        
        logger.info(f"카카오톡 요청 수신 - 사용자: {username}, 메시지: {user_message}")
        
        # username이 "인증서 업로드"인 경우 처리
        if username == "인증서 업로드":
            logger.info("사용자명이 '인증서 업로드'로 설정됨 - 사용자명 재입력 요청")
            return {
                "version": "2.0",
                "template": {
                    "outputs": [
                        {
                            "simpleText": {
                                "text": "인증서 업로드 버튼을 누르고, 사용자명을 다시 입력해주세요.(설정한 카카오톡 이름과 동일해야합니다)"
                            }
                        }
                    ]
                }
            }
        
    
        # 이미지 URL 추출 및 다운로드
        image_urls = extract_image_urls_from_kakao_data(data)
        downloaded_images = []
        saved_files = []  # 저장된 파일 경로 리스트 (롤백용)
        saved_to_db_count = 0
        date_folder = None
        
        if image_urls:
            logger.info(f"발견된 이미지 URL: {len(image_urls)}개")
            
            # 비동기 HTTP 세션으로 이미지 다운로드
            async with aiohttp.ClientSession() as session:
                download_tasks = [
                    download_kakao_image(session, url, user_id, username) 
                    for url in image_urls
                ]
                
                # 모든 다운로드 작업 동시 실행
                if download_tasks:
                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                    
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            logger.error(f"이미지 {i+1} 다운로드 예외: {str(result)}")
                            downloaded_images.append({
                                "status": "error", 
                                "error": str(result),
                                "url": image_urls[i] if i < len(image_urls) else "unknown"
                            })
                        else:
                            downloaded_images.append(result)
                            
                            # 성공한 이미지 경로 저장
                            if result.get("status") == "success":
                                saved_files.append(result.get("file_path"))
                                if not date_folder:
                                    date_folder = result.get("date_folder")
        
        # 성공한 다운로드 수 계산
        success_count = sum(1 for img in downloaded_images if img.get("status") == "success")
        
        # 모든 이미지가 성공적으로 다운로드되지 않은 경우 롤백
        if image_urls and success_count != len(image_urls):
            logger.warning(f"이미지 처리 실패 - 성공: {success_count}/{len(image_urls)}개, 롤백 시작")
            
            # 저장된 파일들 삭제 (롤백)
            for file_path in saved_files:
                try:
                    if file_path and Path(file_path).exists():
                        Path(file_path).unlink()
                        logger.info(f"롤백: 파일 삭제 - {file_path}")
                except Exception as e:
                    logger.error(f"파일 삭제 실패 - {file_path}: {str(e)}")
            
            # 실패 응답 반환
            return {
                "version": "2.0",
                "template": {
                    "outputs": [
                        {
                            "simpleText": {
                                "text": f"❌ 접속 증가로 오류가 발생하였습니다.\n 잠시후 다시 인증서를 업로드 해주세요."
                            }
                        }
                    ]
                }
            }
        
        # 모든 이미지가 성공한 경우에만 DB 저장
        if success_count > 0:
            for i, img in enumerate(downloaded_images):
                if img.get("status") == "success":
                    db_saved = await save_image_upload_to_db(
                        username=username,
                        original_url=image_urls[i],
                        user_id=user_id,
                        image_data=img
                    )
                    if db_saved:
                        saved_to_db_count += 1
        
        # 응답 텍스트 생성
        response_text = format_request_summary(data, success_count, len(image_urls), date_folder)
        
        logger.info(f"카카오톡 요청 처리 완료 - 사용자: {user_id}, 이미지: {success_count}/{len(image_urls)}개, DB저장: {saved_to_db_count}개")
        
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