import os
import sys
import json
import time
import requests
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import simpledialog, messagebox
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

ORG_ID = os.getenv("ORG_ID")
API_TOKEN = os.getenv("API_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.atlassian.com/admin/v1/orgs")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "100"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE_SECONDS", "2"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
LOGS_DIR = os.getenv("LOGS_DIR", "./logs")


if not ORG_ID or not API_TOKEN:
    """
    - 필수 환경변수 확인
    - 종료 코드 2 반환(기타 오류)
    """
    print("ERROR: ORG_ID and API_TOKEN must be set in .env", file=sys.stderr)
    sys.exit(2)

os.makedirs(LOGS_DIR, exist_ok=True)


def request_with_retries(url: str, headers: dict, params: dict = None) -> dict:
    """
    요청 함수: 재시도 로직 포함, 요청이 실패하거나 429 응답이 오면 재시도
    - url: 요청 URL
    - headers: 요청 헤더
    - params: 쿼리 파라미터
    - 반환: 응답 JSON 딕셔너리
    - 예외: 요청이 계속 실패하면 requests.RequestException 발생
    """
    attempt = 0
    while True:
        attempt += 1
        default_retry_seconds = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            print(f"[Request 요청] Page Size 확인 : {resp.json().get('meta',{}).get('page_size',{})}")
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                wait = int(ra) if ra and ra.isdigit() else default_retry_seconds
                print(f"Rate limited (429). Waiting {wait} seconds...", file=sys.stderr)
                time.sleep(wait)
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
            else :
                resp.raise_for_status()
                return resp.json()
        except requests.RequestException as e:
            print(f"Request attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt >= MAX_RETRIES:
                raise
            backoff = default_retry_seconds
            time.sleep(backoff)

def build_initial_uri() -> str:
    """
    초기 URI 빌드
    """
    base = API_BASE_URL.rstrip("/")
    return f"{base}/{ORG_ID}/events-stream"

def extract_events_from_response(resp_json: dict):
    """
    이벤트 데이터 추출 \n
    data 필드에서 이벤트 리스트 반환, 없으면 빈 리스트 반환
    """
    return resp_json.get("data", [])


def get_next_cursor_from_response(resp_json: dict):
    """
    페이지네이션을 위한 다음 커서 추출
    meta.next 또는 links.next 사용
    """
    meta = resp_json.get("meta", {})
    if meta and meta.get("next"):
        return meta.get("next")
    links = resp_json.get("links", {})
    if links and links.get("next"):
        return links.get("next")
    return None


def append_jsonline(path: str, obj: dict):
    """
    json 라인 형식으로 객체 파일에 추가
    """
    line = json.dumps(obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def ask_log_info():
    """
    사용자에게 로그 파일명과 날짜 범위를 입력받는 팝업 창 생성\n
    - 반환: {"filename": str, "date_from": str, "date_to": str} \n
    - 날짜 형식은 "YYYY-MM-DD"
    >>> 잘못된 입력 시 오류 메시지 표시 후 재입력 요구
    """
    result = {}
    KST = timezone(timedelta(hours=9))  # 한국 표준시 UTC+9

    def epoch_time(date_str:str,*,start_or_end:str) -> int | None:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            match start_or_end:
                case "start":
                    dt = dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=KST)
                case "end":
                    dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=KST)
                case _:
                    print("Invalid start_or_end value", file=sys.stderr)
                    on_close()
                    return None
            
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None

    def on_submit():
        filename = filename_entry.get().strip().removesuffix(".log")
        date_from = date_from_entry.get().strip()
        date_to = date_to_entry.get().strip()

        # 입력값 검증
        if not filename or not date_from or not date_to:
            messagebox.showerror("입력 오류", "모든 항목을 입력해주세요.")
            return
        date_from = epoch_time(date_from,start_or_end="start")
        date_to = epoch_time(date_to,start_or_end="end")

        if not date_from or not date_to:
            messagebox.showerror("날짜 형식 오류", "날짜는 YYYY-MM-DD 형식으로 입력해주세요.")
            return

        result["filename"] = filename
        result["date_from"] = date_from
        result["date_to"] = date_to
        popup.destroy()

    def on_close():
        # X 버튼 눌렀을 때 프로그램 전체 종료
        popup.destroy()
        sys.exit()   # 프로그램 완전히 종료

    popup = tk.Tk()
    popup.title("로그 정보 입력")
    popup.geometry("300x200")
    popup.resizable(False, False)

    # X 버튼 동작 지정
    popup.protocol("WM_DELETE_WINDOW", on_close)

    # 가상의 박스(Frame) 생성
    form_frame = tk.Frame(popup)
    form_frame.pack(expand=True)  # 팝업창 가운데에 위치
    
    # Frame 안에 라벨과 입력창 배치
    # 왼쪽 정렬
    tk.Label(form_frame, text="파일명").pack(anchor="w", padx=10, pady=(5,0))
    filename_entry = tk.Entry(form_frame)
    filename_entry.pack(anchor="w", padx=10, pady=(0,5))

    tk.Label(form_frame, text="시작 날짜 (YYYY-MM-DD)").pack(anchor="w", padx=10, pady=(5,0))
    date_from_entry = tk.Entry(form_frame)
    date_from_entry.pack(anchor="w", padx=10, pady=(0,5))

    tk.Label(form_frame, text="종료 날짜 (YYYY-MM-DD)").pack(anchor="w", padx=10, pady=(5,0))
    date_to_entry = tk.Entry(form_frame)
    date_to_entry.pack(anchor="w", padx=10, pady=(0,5))

    submit_btn = tk.Button(form_frame, text="확인", command=on_submit)
    submit_btn.pack(padx=10, pady=5)

    popup.mainloop()
    return result



def run_main():
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    uri = build_initial_uri()
    user_input = ask_log_info()
    print(user_input)

    params = {"limit": PAGE_SIZE, "from": user_input.get("date_from"), "to":user_input.get("date_to")} # 초기에는 .env의 값을 사용
    
    log_path = os.path.join(LOGS_DIR, f"{user_input.get('filename')}.log")

    total_count = 0

    while True:
        resp_json = request_with_retries(uri, headers, params=params)
        events_data = extract_events_from_response(resp_json)

        for item in events_data:
            rec = {
                "time": item.get("attributes", {}).get("time"),
                "action": item.get("attributes", {}).get("action"),
                "actor_name": item.get("attributes", {}).get("actor").get("name"),
                "actor_email": item.get("attributes", {}).get("actor").get("email"),
                "ip": item.get("attributes", {}).get("location", {}).get("ip"),
                "event_id": item.get("id")
            }
            append_jsonline(log_path, rec)
            total_count += 1

        next_token = get_next_cursor_from_response(resp_json)
        if not next_token:
            break

        # next_token이 이미 전체 URL이므로, limit, cursor 같은 파라미터가 포함되어 있을 수 있기 때문.
        # ex. links.next: "https://api.atlassian.com/admin/v1/orgs/{ORG_ID}/events?limit=100&cursor=abcd"
        if isinstance(next_token, str) and next_token.startswith("http"):
            uri = next_token
            params = None
        else:
            uri = build_initial_uri()
            params = {"limit": PAGE_SIZE,"from": user_input.get("date_from"), "to":user_input.get("date_to"), "cursor": next_token}

    print(f"수집 완료: {total_count}개의 이벤트를 {log_path}에 저장했습니다.")

if __name__ == "__main__":
    run_main()