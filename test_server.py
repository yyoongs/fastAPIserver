#!/usr/bin/env python3
"""
서버 테스트 및 상태 확인 스크립트
"""

import requests
import time
import sys
from pathlib import Path

def test_server_connection():
    """서버 연결 테스트"""
    print("🔍 서버 연결 상태 확인 중...")
    
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        if response.status_code == 200:
            print("✅ 서버가 정상적으로 실행 중입니다!")
            data = response.json()
            print(f"📊 서버 정보:")
            print(f"   - 상태: {data.get('status')}")
            print(f"   - 시간: {data.get('timestamp')}")
            print(f"   - 업로드 디렉토리: {data.get('upload_dir')}")
            print(f"   - 최대 파일 크기: {data.get('max_file_size_mb')}MB")
            print(f"   - 지원 확장자: {', '.join(data.get('allowed_extensions', []))}")
            return True
        else:
            print(f"❌ 서버 응답 오류: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ 서버에 연결할 수 없습니다.")
        print("   - 서버가 실행 중인지 확인해주세요.")
        print("   - python3 main.py 명령으로 서버를 시작하세요.")
        return False
    except requests.exceptions.Timeout:
        print("❌ 서버 응답 시간이 초과되었습니다.")
        return False
    except Exception as e:
        print(f"❌ 예상치 못한 오류: {e}")
        return False

def test_file_upload():
    """간단한 파일 업로드 테스트"""
    print("\n📤 테스트 이미지 업로드 시도...")
    
    # 간단한 테스트 이미지 생성 (1x1 픽셀 PNG)
    test_image_data = (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x01\x00\x00\x00\x007n\xf9$\x00\x00'
        b'\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xdb'
        b'\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    
    try:
        files = {'file': ('test.png', test_image_data, 'image/png')}
        response = requests.post("http://localhost:8000/upload/single", files=files, timeout=10)
        
        if response.status_code == 200:
            print("✅ 테스트 파일 업로드 성공!")
            data = response.json()
            print(f"   - 원본 파일명: {data['data']['original_filename']}")
            print(f"   - 저장된 파일명: {data['data']['saved_filename']}")
            print(f"   - 파일 크기: {data['data']['file_size']} bytes")
            return True
        else:
            print(f"❌ 파일 업로드 실패: {response.status_code}")
            print(f"   응답: {response.text}")
            return False
    except Exception as e:
        print(f"❌ 파일 업로드 테스트 실패: {e}")
        return False

def check_uploaded_files():
    """업로드된 파일 목록 확인"""
    print("\n📋 업로드된 파일 목록 확인...")
    
    try:
        response = requests.get("http://localhost:8000/files", timeout=5)
        if response.status_code == 200:
            data = response.json()
            file_count = data.get('total_files', 0)
            print(f"📊 총 {file_count}개의 파일이 업로드되어 있습니다.")
            
            if file_count > 0:
                print("📁 최근 파일들:")
                for i, file_info in enumerate(data.get('files', [])[:5]):  # 최대 5개만 표시
                    size_mb = file_info['size'] / (1024 * 1024)
                    print(f"   {i+1}. {file_info['filename']} ({size_mb:.2f}MB)")
            
            return True
        else:
            print(f"❌ 파일 목록 조회 실패: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 파일 목록 확인 실패: {e}")
        return False

def monitor_server():
    """서버 실시간 모니터링"""
    print("\n🔄 서버 실시간 모니터링 시작 (Ctrl+C로 중지)")
    print("=" * 50)
    
    try:
        while True:
            try:
                # 헬스체크 요청
                start_time = time.time()
                response = requests.get("http://localhost:8000/health", timeout=3)
                response_time = (time.time() - start_time) * 1000
                
                if response.status_code == 200:
                    current_time = time.strftime("%H:%M:%S")
                    print(f"✅ {current_time} - 서버 정상 (응답시간: {response_time:.1f}ms)")
                else:
                    print(f"⚠️  {time.strftime('%H:%M:%S')} - 서버 응답 오류: {response.status_code}")
                
            except requests.exceptions.ConnectionError:
                print(f"❌ {time.strftime('%H:%M:%S')} - 서버 연결 불가")
            except requests.exceptions.Timeout:
                print(f"⏰ {time.strftime('%H:%M:%S')} - 서버 응답 시간 초과")
            except Exception as e:
                print(f"❌ {time.strftime('%H:%M:%S')} - 오류: {e}")
            
            time.sleep(5)  # 5초마다 체크
            
    except KeyboardInterrupt:
        print(f"\n🛑 모니터링을 중지합니다.")

def main():
    """메인 실행 함수"""
    print("=" * 70)
    print("🧪 FastAPI 이미지 업로드 서버 테스트 도구")
    print("=" * 70)
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == "monitor":
            monitor_server()
            return
        elif command == "upload":
            test_file_upload()
            return
        elif command == "files":
            check_uploaded_files()
            return
        elif command == "help":
            print("사용법:")
            print("  python3 test_server.py           # 전체 테스트 실행")
            print("  python3 test_server.py monitor   # 실시간 모니터링")
            print("  python3 test_server.py upload    # 업로드 테스트만")
            print("  python3 test_server.py files     # 파일 목록만 확인")
            print("  python3 test_server.py help      # 도움말")
            return
    
    # 전체 테스트 실행
    print("🏃 전체 테스트를 시작합니다...\n")
    
    # 1. 서버 연결 테스트
    if not test_server_connection():
        print("\n💡 서버를 시작하려면: python3 main.py")
        print("💡 서버 상태를 확인하려면: python3 test_server.py monitor")
        return
    
    # 2. 파일 업로드 테스트
    test_file_upload()
    
    # 3. 파일 목록 확인
    check_uploaded_files()
    
    print("\n" + "=" * 70)
    print("✅ 테스트 완료!")
    print("💡 실시간 모니터링: python3 test_server.py monitor")
    print("💡 웹 브라우저에서 확인: http://localhost:8000/docs")
    print("=" * 70)

if __name__ == "__main__":
    main()