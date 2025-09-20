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

def get_kst_timestamp() -> str:
    """한국 시간 타임스탬프 반환 (파일명용)"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst).strftime('%Y%m%d_%H%M%S')

def get_kst_datetime() -> datetime:
    """한국 시간 datetime 객체 반환"""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)

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
    
    insert_sql = """
    INSERT INTO kakao_image_uploads (
        username, original_url, user_id, filename, file_path, 
        file_size, content_type, upload_time
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    try:
        async with db_pool.connection() as conn:
            await conn.execute(
                insert_sql,
                (
                    username,
                    original_url,
                    user_id,
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

def generate_unique_filename(user_id: str, extension: str = ".jpg") -> str:
    """고유한 파일명 생성"""
    timestamp = get_kst_timestamp()
    unique_id = str(uuid.uuid4())[:8]
    return f"kakao_{user_id[:8]}_{timestamp}_{unique_id}{extension}"

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
        
        # URL 디코딩
        urls = [unquote(url) for url in urls]
        
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
        
        # 파일명 생성 및 저장
        filename = generate_unique_filename(user_id, extension)
        file_path = KAKAO_IMAGE_DIR / filename
        
        # 파일 저장
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(image_data)
        
        logger.info(f"이미지 저장 완료: {filename} ({len(image_data):,} bytes)")
        
        return {
            "status": "success",
            "filename": filename,
            "file_path": str(file_path),
            "file_size": len(image_data),
            "content_type": content_type,
            "original_url": url
        }
        
    except asyncio.TimeoutError:
        logger.error("이미지 다운로드 타임아웃")
        return {"status": "error", "error": "Download timeout"}
    except Exception as e:
        logger.error(f"이미지 다운로드 실패: {str(e)}")
        return {"status": "error", "error": str(e)}

def format_request_summary(data: Dict[Any, Any], success_count: int, total_images: int) -> str:
    """요청 정보를 요약 형태로 포맷팅"""
    intent = data["intent"]
    user_request = data["userRequest"]
    action = data["action"]
    user_properties = user_request["user"].get("properties", {})
    username = user_properties.get("username", "Unknown")
    
    summary = f"""요청 처리 완료

사용자: {username}
메시지: "{user_request['utterance']}"
처리 시간: {get_kst_time()}"""

    if total_images > 0:
        summary += f"""
이미지 처리: {success_count}/{total_images}개 성공"""
    
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
        user_type = user_request["user"]["type"]
        
        # action params에서 username 추출
        action_params = data.get("action", {}).get("params", {})
        username = action_params.get("username", "Unknown")
        
        logger.info(f"카카오톡 요청 수신 - 사용자: {username}, 메시지: {user_message}")
        
        # 이미지 URL 추출 및 다운로드
        image_urls = extract_image_urls_from_kakao_data(data)
        downloaded_images = []
        saved_to_db_count = 0
        
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
                            
                            # 성공한 이미지만 DB에 저장
                            if result.get("status") == "success":
                                db_saved = await save_image_upload_to_db(
                                    username=username,
                                    original_url=image_urls[i],
                                    user_id=user_id,
                                    image_data=result
                                )
                                if db_saved:
                                    saved_to_db_count += 1
        
        # 성공한 다운로드 수 계산
        success_count = sum(1 for img in downloaded_images if img.get("status") == "success")
        
        # 응답 텍스트 생성
        response_text = format_request_summary(data, success_count, len(image_urls))
        if saved_to_db_count > 0:
            response_text += f"\nDB 저장: {saved_to_db_count}개 완료"
        
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
            workers=1,
            reload=True,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n" + "=" * 70)
        print("서버가 안전하게 종료되었습니다.")
        print("=" * 70)