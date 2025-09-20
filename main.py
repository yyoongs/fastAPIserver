from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request
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
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
MAX_CONCURRENT_UPLOADS = 1000  # 동시 업로드 제한

# 업로드 디렉토리 생성
UPLOAD_DIR.mkdir(exist_ok=True)
logger.info(f"업로드 디렉토리 생성/확인 완료: {UPLOAD_DIR.absolute()}")

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
async def upload_single_image(file: UploadFile = File(...)):
    """단일 이미지 업로드"""
    async with upload_semaphore:
        # 파일 검증
        if not file.filename:
            raise HTTPException(status_code=400, detail="파일명이 없습니다")
        
        if not is_valid_image_type(file.filename):
            raise HTTPException(
                status_code=400, 
                detail=f"지원하지 않는 파일 형식입니다. 지원 형식: {', '.join(ALLOWED_EXTENSIONS)}"
            )
        
        # 파일 크기 검증
        file_content = await file.read()
        if len(file_content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413, 
                detail=f"파일 크기가 너무 큽니다. 최대 크기: {MAX_FILE_SIZE // (1024*1024)}MB"
            )
        
        if len(file_content) == 0:
            raise HTTPException(status_code=400, detail="빈 파일입니다")
        
        # 고유 파일명 생성 및 저장
        unique_filename = generate_unique_filename(file.filename)
        saved_path = await save_image_async(file_content, unique_filename)
        
        return JSONResponse({
            "status": "success",
            "message": "파일 업로드 완료",
            "data": {
                "original_filename": file.filename,
                "saved_filename": unique_filename,
                "file_path": saved_path,
                "file_size": len(file_content),
                "upload_time": datetime.now().isoformat()
            }
        })

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

@app.delete("/files/{filename}")
async def delete_file(filename: str):
    """파일 삭제"""
    logger.info(f"🗑️ 파일 삭제 요청: {filename}")
    
    file_path = UPLOAD_DIR / filename
    
    if not file_path.exists():
        logger.warning(f"❌ 삭제할 파일을 찾을 수 없음: {filename}")
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다")
    
    try:
        file_path.unlink()
        logger.info(f"✅ 파일 삭제 완료: {filename}")
        return JSONResponse({
            "status": "success",
            "message": f"파일 삭제 완료: {filename}"
        })
    except Exception as e:
        logger.error(f"❌ 파일 삭제 실패 {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"파일 삭제 실패: {str(e)}")

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
            "single_upload": "/upload/single",
            "multiple_upload": "/upload/multiple",
            "list_files": "/files",
            "delete_file": "/files/{filename}",
            "download_all": "/download/all",
            "kakao_chat": "/kakao/chat",
            "kakao_debug": "/kakao/debug",
            "health_check": "/health"
        }
    })

@app.post("/kakao/chat")
async def process_kakao_request(request: Request):
    """카카오톡 챗봇 요청 처리 및 정리"""
    try:
        # JSON 데이터 받기
        data = await request.json()
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
        
        # 사용자 발화 내용 기반으로 응답 생성
        user_message = user_request["utterance"]
        user_id = user_request["user"]["id"]
        user_type = user_request["user"]["type"]
        user_properties = user_request["user"].get("properties", {})
        bot_name = bot["name"]
        intent_name = intent["name"]
        block_name = user_request["block"]["name"]
        timezone = user_request["timezone"]
        request_params = user_request.get("params", {})
        action_name = action["name"]
        
        # 사용자 속성 정보를 문자열로 변환
        properties_text = ""
        if user_properties:
            properties_list = [f"{k}: {v}" for k, v in user_properties.items()]
            properties_text = f"\n- 사용자 속성: {', '.join(properties_list)}"
        
        # 요청 파라미터 정보를 문자열로 변환
        params_text = ""
        if request_params:
            params_list = [f"{k}: {v}" for k, v in request_params.items()]
            params_text = f"\n- 요청 파라미터: {', '.join(params_list)}"
        
        # 상세한 응답 텍스트 생성 (모든 사용자 정보 포함)
        response_text = f"""안녕하세요! '{user_message}' 메시지를 잘 받았습니다.

📊 전달받은 정보:
- 사용자 ID: {user_id}
- 사용자 타입: {user_type}{properties_text}
- 봇 이름: {bot_name}
- 의도: {intent_name}
- 블록: {block_name}
- 액션: {action_name}
- 시간대: {timezone}{params_text}
- 처리 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

✅ 모든 정보가 정상적으로 수신되었습니다."""
        
        logger.info(f"✅ 카카오톡 요청 처리 완료 - 사용자: {user_id} ({user_type}), 발화: '{user_message[:50]}...', 속성: {len(user_properties)}개")
        
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
        def generate_zip():
            yield zip_buffer.read()
        
        return StreamingResponse(
            io.BytesIO(zip_buffer.getvalue()),
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except Exception as e:
        logger.error(f"❌ 전체 파일 다운로드 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"파일 다운로드 실패: {str(e)}")

@app.get("/download/info")
async def download_info():
    """다운로드 가능한 파일 정보"""
    logger.info("📋 다운로드 정보 요청")
    
    try:
        files = []
        total_size = 0
        
        for file_path in UPLOAD_DIR.glob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                file_size = stat.st_size
                total_size += file_size
                
                files.append({
                    "filename": file_path.name,
                    "size": file_size,
                    "size_mb": round(file_size / (1024 * 1024), 2),
                    "created_time": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "extension": file_path.suffix.lower()
                })
        
        # 파일 타입별 통계
        extensions = {}
        for file_info in files:
            ext = file_info["extension"]
            if ext not in extensions:
                extensions[ext] = {"count": 0, "total_size": 0}
            extensions[ext]["count"] += 1
            extensions[ext]["total_size"] += file_info["size"]
        
        return JSONResponse({
            "status": "success",
            "download_info": {
                "total_files": len(files),
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "file_types": {
                    ext: {
                        "count": info["count"],
                        "total_size_mb": round(info["total_size"] / (1024 * 1024), 2)
                    }
                    for ext, info in extensions.items()
                },
                "estimated_zip_name": f"uploaded_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            },
            "files": files
        })
        
    except Exception as e:
        logger.error(f"❌ 다운로드 정보 조회 실패: {str(e)}")
        raise HTTPException(status_code=500, detail=f"다운로드 정보 조회 실패: {str(e)}")

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
    print("   - POST /upload/single      : 단일 파일 업로드")
    print("   - POST /upload/multiple    : 다중 파일 업로드")
    print("   - GET  /files             : 업로드된 파일 목록")
    print("   - DELETE /files/{name}     : 파일 삭제")
    print("   - GET  /download/all       : 모든 파일 ZIP 다운로드")
    print("   - GET  /download/info      : 다운로드 정보")
    print("   - POST /kakao/chat         : 카카오톡 챗봇 응답 (표준 형식)")
    print("   - POST /kakao/debug        : 카카오톡 요청 분석 (디버깅용)")
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