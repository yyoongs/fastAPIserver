from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import aiofiles
import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path
import logging
from typing import List, Optional, Dict, Any
import mimetypes
import sys
import zipfile
import io
import json
import re
import aiohttp
from typing import Dict, Any, Optional

# 로깅 설정 강화
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('server.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="High Performance Image Upload API",
    description="1만명 동시 요청을 처리할 수 있는 이미지 업로드 서버",
    version="1.0.0"
)

# CORS 설정 (필요시)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 설정
UPLOAD_DIR = Path("uploads")
KAKAO_IMAGE_DIR = Path("/Authfiles/kakao_images")  # 마운트된 디스크 경로
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
MAX_CONCURRENT_UPLOADS = 1000  # 동시 업로드 제한

# 업로드 디렉토리 생성
UPLOAD_DIR.mkdir(exist_ok=True)
KAKAO_IMAGE_DIR.mkdir(parents=True, exist_ok=True)  # 카카오 이미지 디렉토리 생성
logger.info(f"업로드 디렉토리 생성/확인 완료: {UPLOAD_DIR.absolute()}")
logger.info(f"카카오 이미지 디렉토리 생성/확인 완료: {KAKAO_IMAGE_DIR.absolute()}")

# 세마포어로 동시 업로드 수 제한
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
logger.info(f"동시 업로드 제한 설정: {MAX_CONCURRENT_UPLOADS}개")

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

def is_valid_image_type(filename: str) -> bool:
    """파일 확장자 검증"""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS

def generate_unique_filename(original_filename: str) -> str:
    """고유한 파일명 생성"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = str(uuid.uuid4())[:8]
    extension = Path(original_filename).suffix.lower()
    return f"{timestamp}_{unique_id}{extension}"

async def save_image_async(file_content: bytes, filename: str) -> str:
    """비동기로 이미지 파일 저장"""
    file_path = UPLOAD_DIR / filename
    
    try:
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_content)
        logger.info(f"파일 저장 완료: {filename}")
        return str(file_path)
    except Exception as e:
        logger.error(f"파일 저장 실패 {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"파일 저장 실패: {str(e)}")

def extract_image_urls_from_kakao_data(data: Dict[Any, Any]) -> List[str]:
    """카카오톡 데이터에서 이미지 URL들 추출"""
    urls = []
    
    try:
        # detailParams에서 secureUrls 추출
        detail_params = data.get("action", {}).get("detailParams", {})
        
        # secureUrls가 문자열로 되어 있는 경우 (예: "List(http://...)")
        secure_urls_str = detail_params.get("secureUrls", "")
        if secure_urls_str:
            # URL 패턴으로 추출
            url_pattern = r'https?://[^\s,)\]"]+'
            found_urls = re.findall(url_pattern, secure_urls_str)
            urls.extend(found_urls)
        
        # 다른 가능한 위치에서도 URL 찾기
        for key, value in detail_params.items():
            if isinstance(value, str) and ("http" in value):
                found_urls = re.findall(r'https?://[^\s,)\]"]+', value)
                urls.extend(found_urls)
        
        # 중복 제거
        urls = list(set(urls))
        
    except Exception as e:
        logger.error(f"URL 추출 실패: {str(e)}")
    
    return urls

async def download_kakao_image(session: aiohttp.ClientSession, url: str, user_id: str, username: str) -> Dict[str, Any]:
    """카카오톡 이미지 다운로드 및 저장"""
    try:
        logger.info(f"🌐 이미지 다운로드 시작: {url[:100]}...")
        
        # 이미지 다운로드
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                logger.error(f"❌ 이미지 다운로드 실패: HTTP {response.status}")
                return {"status": "error", "error": f"HTTP {response.status}"}
            
            # Content-Type 확인
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                logger.error(f"❌ 이미지가 아닌 파일: {content_type}")
                return {"status": "error", "error": f"Invalid content type: {content_type}"}
            
            # 이미지 데이터 읽기
            image_data = await response.read()
            
            if len(image_data) == 0:
                logger.error("❌ 빈 이미지 파일")
                return {"status": "error", "error": "Empty image file"}
            
            if len(image_data) > MAX_FILE_SIZE:
                logger.error(f"❌ 파일 크기 초과: {len(image_data):,} bytes")
                return {"status": "error", "error": f"File too large: {len(image_data):,} bytes"}
        
        # 파일명 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        
        # 확장자 결정 (Content-Type 기반)
        extension = ".jpg"  # 기본값
        if "png" in content_type:
            extension = ".png"
        elif "gif" in content_type:
            extension = ".gif"
        elif "webp" in content_type:
            extension = ".webp"
        
        filename = f"kakao_{user_id[:8]}_{timestamp}_{unique_id}{extension}"
        file_path = KAKAO_IMAGE_DIR / filename
        
        # 파일 저장
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(image_data)
        
        logger.info(f"✅ 이미지 저장 완료: {filename} ({len(image_data):,} bytes)")
        
        return {
            "status": "success",
            "filename": filename,
            "file_path": str(file_path),
            "file_size": len(image_data),
            "content_type": content_type,
            "original_url": url
        }
        
    except asyncio.TimeoutError:
        logger.error("❌ 이미지 다운로드 타임아웃")
        return {"status": "error", "error": "Download timeout"}
    except Exception as e:
        logger.error(f"❌ 이미지 다운로드 실패: {str(e)}")
        return {"status": "error", "error": str(e)}

@app.on_event("startup")
async def startup_event():
    """서버 시작 시 실행되는 이벤트"""
    logger.info("=" * 60)
    logger.info("🚀 고성능 이미지 업로드 API 서버 시작!")
    logger.info("=" * 60)
    logger.info(f"📁 업로드 디렉토리: {UPLOAD_DIR.absolute()}")
    logger.info(f"📏 최대 파일 크기: {MAX_FILE_SIZE // (1024*1024)}MB")
    logger.info(f"📋 지원 파일 형식: {', '.join(ALLOWED_EXTENSIONS)}")
    logger.info(f"⚡ 최대 동시 업로드: {MAX_CONCURRENT_UPLOADS}개")
    logger.info(f"🌐 서버 주소: http://0.0.0.0:8000")
    logger.info("📖 API 문서: http://0.0.0.0:8000/docs")
    logger.info("=" * 60)

@app.on_event("shutdown")
async def shutdown_event():
    """서버 종료 시 실행되는 이벤트"""
    logger.info("🛑 서버 종료 중...")
    logger.info("👋 안전하게 종료되었습니다.")

@app.post("/upload/multiple")
async def upload_multiple_images(files: List[UploadFile] = File(...)):
    """다중 이미지 업로드"""
    file_count = len(files)
    logger.info(f"📤 다중 파일 업로드 요청: {file_count}개 파일")
    
    if file_count > 20:  # 한 번에 최대 20개 파일
        logger.warning(f"❌ 파일 개수 초과: {file_count}개")
        raise HTTPException(status_code=400, detail="한 번에 최대 20개 파일까지 업로드 가능합니다")
    
    async with upload_semaphore:
        upload_tasks = []
        results = []
        
        for i, file in enumerate(files, 1):
            logger.info(f"📋 파일 {i}/{file_count} 처리 중: {file.filename}")
            
            # 파일 검증
            if not file.filename:
                logger.warning(f"❌ 파일 {i}: 파일명 없음")
                results.append({
                    "status": "error",
                    "filename": "unknown",
                    "error": "파일명이 없습니다"
                })
                continue
            
            if not is_valid_image_type(file.filename):
                logger.warning(f"❌ 파일 {i}: 지원하지 않는 형식 - {file.filename}")
                results.append({
                    "status": "error",
                    "filename": file.filename,
                    "error": "지원하지 않는 파일 형식입니다"
                })
                continue
            
            # 파일 내용 읽기
            file_content = await file.read()
            file_size = len(file_content)
            
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"❌ 파일 {i}: 크기 초과 - {file_size:,} bytes")
                results.append({
                    "status": "error",
                    "filename": file.filename,
                    "error": f"파일 크기가 너무 큽니다. 최대: {MAX_FILE_SIZE // (1024*1024)}MB"
                })
                continue
            
            if file_size == 0:
                logger.warning(f"❌ 파일 {i}: 빈 파일 - {file.filename}")
                results.append({
                    "status": "error",
                    "filename": file.filename,
                    "error": "빈 파일입니다"
                })
                continue
            
            # 업로드 태스크 생성
            unique_filename = generate_unique_filename(file.filename)
            logger.info(f"💾 파일 {i} 저장 준비: {file.filename} -> {unique_filename} ({file_size:,} bytes)")
            
            upload_tasks.append({
                "task": save_image_async(file_content, unique_filename),
                "original_filename": file.filename,
                "unique_filename": unique_filename,
                "file_size": file_size
            })
        
        # 모든 업로드 태스크를 동시에 실행
        logger.info(f"🚀 {len(upload_tasks)}개 파일 동시 저장 시작")
        
        for i, task_info in enumerate(upload_tasks, 1):
            try:
                saved_path = await task_info["task"]
                logger.info(f"✅ 파일 {i}/{len(upload_tasks)} 저장 완료: {task_info['unique_filename']}")
                results.append({
                    "status": "success",
                    "original_filename": task_info["original_filename"],
                    "saved_filename": task_info["unique_filename"],
                    "file_path": saved_path,
                    "file_size": task_info["file_size"],
                    "upload_time": datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"❌ 파일 {i}/{len(upload_tasks)} 저장 실패: {task_info['original_filename']} - {str(e)}")
                results.append({
                    "status": "error",
                    "filename": task_info["original_filename"],
                    "error": str(e)
                })
        
        success_count = sum(1 for r in results if r["status"] == "success")
        logger.info(f"📊 다중 업로드 완료: {success_count}/{file_count} 파일 성공")
        
        return JSONResponse({
            "status": "completed",
            "message": f"{success_count}/{file_count} 파일 업로드 완료",
            "results": results
        })

@app.get("/files")
async def list_uploaded_files():
    """업로드된 파일 목록 조회"""
    logger.info("📋 업로드된 파일 목록 조회 요청")
    
    try:
        files = []
        for file_path in UPLOAD_DIR.glob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                files.append({
                    "filename": file_path.name,
                    "size": stat.st_size,
                    "created_time": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "modified_time": datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
        logger.info(f"📊 파일 목록 조회 완료: {len(files)}개 파일")
        
        return JSONResponse({
            "status": "success",
            "total_files": len(files),
            "files": files
        })
    except Exception as e:
        logger.error(f"❌ 파일 목록 조회 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"파일 목록 조회 실패: {str(e)}")

@app.get("/download/all")
async def download_all_files():
    """업로드된 모든 파일을 ZIP으로 다운로드"""
    logger.info("📦 전체 파일 다운로드 요청")
    
    try:
        # 업로드된 파일 목록 확인
        files = []
        for file_path in UPLOAD_DIR.glob("*"):
            if file_path.is_file():
                files.append(file_path)
        
        if not files:
            logger.warning("❌ 다운로드할 파일이 없음")
            raise HTTPException(status_code=404, detail="다운로드할 파일이 없습니다")
        
        logger.info(f"📊 압축할 파일 수: {len(files)}개")
        
        # ZIP 파일을 메모리에 생성
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in files:
                # ZIP에 파일 추가
                zip_file.write(file_path, file_path.name)
                logger.info(f"📁 압축 추가: {file_path.name}")
        
        zip_buffer.seek(0)
        
        # 현재 시간으로 ZIP 파일명 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"uploaded_files_{timestamp}.zip"
        
        logger.info(f"✅ ZIP 파일 생성 완료: {filename} ({len(files)}개 파일)")
        
        # 스트리밍 응답으로 ZIP 파일 전송
        return StreamingResponse(
            io.BytesIO(zip_buffer.getvalue()),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 전체 파일 다운로드 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"파일 다운로드 실패: {str(e)}")

@app.post("/kakao/chat")
async def process_kakao_request(request: Request):
    """카카오톡 챗봇 요청 처리 및 이미지 저장"""
    try:
        # JSON 데이터 받기
        data = await request.json()
        
        # 요청 전체를 로그에 출력
        logger.info("="*80)
        logger.info("📋 카카오톡 요청 전체 데이터:")
        logger.info(f"{json.dumps(data, indent=2, ensure_ascii=False)}")
        logger.info("="*80)
        
        logger.info(f"💬 카카오톡 챗봇 요청 수신: {data.get('userRequest', {}).get('utterance', 'N/A')}")
        
        # 데이터 유효성 검사
        if not validate_kakao_request(data):
            logger.warning("❌ 잘못된 카카오톡 요청 형식")
            raise HTTPException(status_code=400, detail="잘못된 요청 형식입니다. 카카오톡 챗봇 표준 형식을 확인해주세요.")
        
        # 요청 데이터 정리
        intent = data["intent"]
        user_request = data["userRequest"]
        bot = data["bot"]
        action = data["action"]
        
        # 사용자 정보
        user_message = user_request["utterance"]
        user_id = user_request["user"]["id"]
        user_type = user_request["user"]["type"]
        user_properties = user_request["user"].get("properties", {})
        username = user_properties.get("username", "Unknown")
        
        # 이미지 URL 추출 및 다운로드
        image_urls = extract_image_urls_from_kakao_data(data)
        downloaded_images = []
        
        if image_urls:
            logger.info(f"🖼️ 발견된 이미지 URL: {len(image_urls)}개")
            
            # 비동기 HTTP 세션으로 이미지 다운로드
            async with aiohttp.ClientSession() as session:
                download_tasks = []
                for url in image_urls:
                    task = download_kakao_image(session, url, user_id, username)
                    download_tasks.append(task)
                
                # 모든 다운로드 작업 동시 실행
                if download_tasks:
                    results = await asyncio.gather(*download_tasks, return_exceptions=True)
                    
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            logger.error(f"❌ 이미지 {i+1} 다운로드 예외: {str(result)}")
                            downloaded_images.append({
                                "status": "error", 
                                "error": str(result),
                                "url": image_urls[i] if i < len(image_urls) else "unknown"
                            })
                        else:
                            downloaded_images.append(result)
        
        # 성공한 다운로드 수 계산
        success_count = sum(1 for img in downloaded_images if img.get("status") == "success")
        
        # 전달받은 요청 정보를 텍스트로 변환
        request_text = f"""📋 전달받은 요청 전체:

🎯 Intent (의도):
- ID: {intent['id']}
- Name: {intent['name']}

👤 User Request (사용자 요청):
- Timezone: {user_request['timezone']}
- Language: {user_request.get('lang', 'N/A')}
- Utterance: "{user_request['utterance']}"

📦 Block (블록):
- ID: {user_request['block']['id']}
- Name: {user_request['block']['name']}

👨‍💼 User (사용자):
- ID: {user_request['user']['id']}
- Type: {user_request['user']['type']}"""

        # 사용자 속성 추가
        if user_properties:
            request_text += "\n- Properties:"
            for key, value in user_properties.items():
                request_text += f"\n  • {key}: {value}"
        else:
            request_text += "\n- Properties: 없음"

        # 요청 파라미터 추가
        request_params = user_request.get("params", {})
        request_text += f"\n\n⚙️ Request Params:"
        if request_params:
            for key, value in request_params.items():
                request_text += f"\n- {key}: {value}"
        else:
            request_text += "\n- 없음"

        # 봇 정보 추가
        request_text += f"""

🤖 Bot (봇):
- ID: {bot['id']}
- Name: {bot['name']}

🎬 Action (액션):
- ID: {action['id']}
- Name: {action['name']}
- Client Extra: {action.get('clientExtra', 'N/A')}"""

        # 액션 파라미터 추가
        action_params = action.get('params', {})
        request_text += f"\n- Params:"
        if action_params:
            for key, value in action_params.items():
                request_text += f"\n  • {key}: {value}"
        else:
            request_text += " 없음"

        # 액션 상세 파라미터 추가
        detail_params = action.get('detailParams', {})
        request_text += f"\n- Detail Params:"
        if detail_params:
            for key, value in detail_params.items():
                if len(str(value)) > 100:  # 긴 값은 축약
                    request_text += f"\n  • {key}: {str(value)[:100]}..."
                else:
                    request_text += f"\n  • {key}: {value}"
        else:
            request_text += " 없음"

        # 이미지 처리 결과 추가
        if image_urls:
            request_text += f"""

🖼️ 이미지 처리 결과:
- 발견된 이미지 URL: {len(image_urls)}개
- 다운로드 성공: {success_count}개
- 저장 위치: {KAKAO_IMAGE_DIR}"""
            
            for i, img_result in enumerate(downloaded_images, 1):
                if img_result.get("status") == "success":
                    request_text += f"\n  ✅ 이미지 {i}: {img_result['filename']} ({img_result['file_size']:,} bytes)"
                else:
                    request_text += f"\n  ❌ 이미지 {i}: {img_result.get('error', 'Unknown error')}"

        # 처리 정보 추가
        request_text += f"""

⏰ 처리 정보:
- 처리 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- 서버 상태: 정상 동작 중
- 이미지 저장 경로: {KAKAO_IMAGE_DIR}

✅ 모든 정보가 성공적으로 수신되었습니다."""
        
        # 최종 응답 텍스트
        if image_urls and success_count > 0:
            response_text = f"""안녕하세요! '{user_message}' 메시지와 함께 {success_count}개의 이미지를 성공적으로 저장했습니다.

{request_text}"""
        elif image_urls and success_count == 0:
            response_text = f"""안녕하세요! '{user_message}' 메시지를 받았지만, {len(image_urls)}개의 이미지 저장에 실패했습니다.

{request_text}"""
        else:
            response_text = f"""안녕하세요! '{user_message}' 메시지를 잘 받았습니다.

{request_text}"""
        
        logger.info(f"✅ 카카오톡 요청 처리 완료 - 사용자: {user_id} ({user_type}), 발화: '{user_message[:50]}...', 이미지: {success_count}/{len(image_urls)}개 저장")
        
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
        # HTTPException은 그대로 다시 raise
        raise
    except Exception as e:
        logger.error(f"❌ 카카오톡 요청 처리 실패: {str(e)}")
        # 에러 발생 시에도 카카오톡 표준 형식으로 응답
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
    return JSONResponse({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "upload_dir": str(UPLOAD_DIR),
        "max_file_size_mb": MAX_FILE_SIZE // (1024*1024),
        "allowed_extensions": list(ALLOWED_EXTENSIONS)
    })

@app.get("/")
async def root():
    """루트 경로"""
    return JSONResponse({
        "message": "고성능 이미지 업로드 API 서버",
        "version": "1.0.0",
        "endpoints": {
            "multiple_upload": "/upload/multiple",
            "list_files": "/files",
            "download_all_files": "/download/all",
            "kakao_chat": "/kakao/chat",
            "health_check": "/health"
        }
    })

if __name__ == "__main__":
    import uvicorn
    
    # 시작 메시지
    print("=" * 70)
    print("🚀 고성능 이미지 업로드 API 서버를 시작합니다!")
    print("=" * 70)
    print(f"📁 업로드 디렉토리: {UPLOAD_DIR.absolute()}")
    print(f"📏 최대 파일 크기: {MAX_FILE_SIZE // (1024*1024)}MB")
    print(f"📋 지원 파일 형식: {', '.join(ALLOWED_EXTENSIONS)}")
    print(f"⚡ 최대 동시 업로드: {MAX_CONCURRENT_UPLOADS}개")
    print("=" * 70)
    print("🌐 서버 주소:")
    print("   - 메인: http://localhost:8000")
    print("   - API 문서: http://localhost:8000/docs")
    print("   - 헬스체크: http://localhost:8000/health")
    print("=" * 70)
    print("📖 주요 엔드포인트:")
    print("   - POST /upload/multiple    : 다중 파일 업로드")
    print("   - GET  /files             : 업로드된 파일 목록")
    print("   - GET  /download/all       : 모든 파일 ZIP 다운로드")
    print("   - POST /kakao/chat         : 카카오톡 챗봇 응답 (요청 전체 포함)")
    print("   - GET  /health            : 헬스체크")
    print("=" * 70)
    print("⚠️  서버를 중지하려면 Ctrl+C를 누르세요")
    print("=" * 70)
    
    try:
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8000,
            workers=1,  # 개발용으로 1개 워커 사용
            loop="asyncio",  # 기본 asyncio 사용 (uvloop 제거)
            access_log=True,  # 개발 시 액세스 로그 활성화
            reload=True,  # 코드 변경 시 자동 재시작
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n" + "=" * 70)
        print("🛑 서버가 안전하게 종료되었습니다.")
        print("👋 감사합니다!")
        print("=" * 70)