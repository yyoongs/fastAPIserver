#!/usr/bin/env python3
"""
간단한 서버 테스트 (외부 라이브러리 불필요)
"""

import urllib.request
import urllib.error
import json
import time
import sys

def test_server_simple():
    """requests 없이 서버 연결 테스트"""
    print("🔍 서버 연결 상태 확인 중...")
    
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=5) as response:
            if response.status == 200:
                print("✅ 서버가 정상적으로 실행 중입니다!")
                
                data = json.loads(response.read().decode())
                print(f"📊 서버 정보:")
                print(f"   - 상태: {data.get('status')}")
                print(f"   - 시간: {data.get('timestamp')}")
                print(f"   - 업로드 디렉토리: {data.get('upload_dir')}")
                print(f"   - 최대 파일 크기: {data.get('max_file_size_mb')}MB")
                print(f"   - 지원 확장자: {', '.join(data.get('allowed_extensions', []))}")
                return True
            else:
                print(f"❌ 서버 응답 오류: {response.status}")
                return False
                
    except urllib.error.URLError as e:
        if "Connection refused" in str(e) or "ConnectionRefusedError" in str(e):
            print("❌ 서버에 연결할 수 없습니다.")
            print("   - 서버가 실행 중인지 확인해주세요.")
            print("   - python3 main.py 명령으로 서버를 시작하세요.")
        else:
            print(f"❌ 네트워크 오류: {e}")
        return False
    except Exception as e:
        print(f"❌ 예상치 못한 오류: {e}")
        return False

def check_files_simple():
    """requests 없이 파일 목록 확인"""
    print("\n📋 업로드된 파일 목록 확인...")
    
    try:
        with urllib.request.urlopen("http://localhost:8000/files", timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                file_count = data.get('total_files', 0)
                print(f"📊 총 {file_count}개의 파일이 업로드되어 있습니다.")
                
                if file_count > 0:
                    print("📁 최근 파일들:")
                    for i, file_info in enumerate(data.get('files', [])[:5]):
                        size_mb = file_info['size'] / (1024 * 1024)
                        print(f"   {i+1}. {file_info['filename']} ({size_mb:.2f}MB)")
                
                return True
            else:
                print(f"❌ 파일 목록 조회 실패: {response.status}")
                return False
    except Exception as e:
        print(f"❌ 파일 목록 확인 실패: {e}")
        return False

def monitor_simple():
    """간단한 서버 모니터링"""
    print("\n🔄 서버 실시간 모니터링 시작 (Ctrl+C로 중지)")
    print("=" * 50)
    
    try:
        while True:
            try:
                start_time = time.time()
                with urllib.request.urlopen("http://localhost:8000/health", timeout=3) as response:
                    response_time = (time.time() - start_time) * 1000
                    
                    if response.status == 200:
                        current_time = time.strftime("%H:%M:%S")
                        print(f"✅ {current_time} - 서버 정상 (응답시간: {response_time:.1f}ms)")
                    else:
                        print(f"⚠️  {time.strftime('%H:%M:%S')} - 서버 응답 오류: {response.status}")
                        
            except urllib.error.URLError:
                print(f"❌ {time.strftime('%H:%M:%S')} - 서버 연결 불가")
            except Exception as e:
                print(f"❌ {time.strftime('%H:%M:%S')} - 오류: {e}")
            
            time.sleep(5)  # 5초마다 체크
            
    except KeyboardInterrupt:
        print(f"\n🛑 모니터링을 중지합니다.")

def main():
    """메인 실행 함수"""
    print("=" * 70)
    print("🧪 FastAPI 서버 간단 테스트 도구 (requests 불필요)")
    print("=" * 70)
    
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        
        if command == "monitor":
            monitor_simple()
            return
        elif command == "files":
            check_files_simple()
            return
        elif command == "help":
            print("사용법:")
            print("  python3 simple_test.py           # 서버 연결 테스트")
            print("  python3 simple_test.py monitor   # 실시간 모니터링")
            print("  python3 simple_test.py files     # 파일 목록만 확인")
            print("  python3 simple_test.py help      # 도움말")
            return
    
    # 기본 테스트 실행
    print("🏃 서버 연결 테스트를 시작합니다...\n")
    
    # 서버 연결 테스트
    if not test_server_simple():
        print("\n💡 서버를 시작하려면: python3 main.py")
        print("💡 서버 상태를 확인하려면: python3 simple_test.py monitor")
        return
    
    # 파일 목록 확인
    check_files_simple()
    
    print("\n" + "=" * 70)
    print("✅ 테스트 완료!")
    print("💡 실시간 모니터링: python3 simple_test.py monitor")
    print("💡 웹 브라우저에서 확인: http://localhost:8000/docs")
    print("💡 파일 업로드 테스트는 웹 브라우저의 /docs에서 가능합니다")
    print("=" * 70)

if __name__ == "__main__":
    main()