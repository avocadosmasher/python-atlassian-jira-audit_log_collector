import os
import sys
import json
import time
import csv
import requests
import threading
import queue
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

ORG_ID = os.getenv("ORG_ID")
API_TOKEN = os.getenv("API_TOKEN")
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.atlassian.com/admin/v1/orgs")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "500"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_SECONDS = int(os.getenv("RETRY_BASE_SECONDS", "3"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
LOGS_DIR = os.getenv("LOGS_DIR", "./logs")

if not ORG_ID or not API_TOKEN:
    print("ERROR: ORG_ID and API_TOKEN must be set in .env", file=sys.stderr)
    sys.exit(2)

os.makedirs(LOGS_DIR, exist_ok=True)

# UI 로그 큐 (스레드 안전)
ui_log_queue = queue.Queue()

def request_with_retries(url: str, headers: dict, params: dict = None) -> dict:
    attempt = 0
    while True:
        attempt += 1
        default_retry_seconds = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            # 안전하게 JSON 구문 확인 (일부 429 응답엔 바디가 없을 수 있음)
            if resp.status_code == 429:
                # 확인 결과 실재로 Retry-After 헤더가 넘어오지는 않음...
                ra = resp.headers.get("Retry-After")
                wait = int(ra) if ra and ra.isdigit() else default_retry_seconds
                ui_log_queue.put(f"[429] Rate limited. Waiting {wait} seconds (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                ui_log_queue.put(f"[Error] The result of request is empty (attempt {attempt})")
                return {}
        except requests.RequestException as e:
            ui_log_queue.put(f"[Request error] attempt {attempt}: {e}")

            # 429 이외의 에러에 대해 attempt 제한 적용
            if attempt >= MAX_RETRIES:
                raise
            ui_log_queue.put(f"Waiting {default_retry_seconds} seconds before retry")
            time.sleep(default_retry_seconds)

def build_initial_uri() -> str:
    base = API_BASE_URL.rstrip("/")
    return f"{base}/{ORG_ID}/events-stream"

def extract_events_from_response(resp_json: dict):
    """
    응답 JSON에서 이벤트 data(REST API 응답의 최상위 객체중 중 하나) 배열 추출 \n
    반환 값: 이벤트 data 리스트
    """
    return resp_json.get("data", [])

def get_next_cursor_from_response(resp_json: dict):
    meta = resp_json.get("meta", {})
    if meta and meta.get("next"):
        return meta.get("next")
    links = resp_json.get("links", {})
    if links and links.get("next"):
        return links.get("next")
    return None

def append_jsonline(path: str, obj: dict):
    """
    path에 있는 파일에 전달된 obj를 jsonlines 형식으로 쓰기\n
    파일이 없으면 생성함
    """
    # UTF-8로 인코딩된 jsonlines 형식으로 기록
    line = json.dumps(obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def convert_log_to_csv(log_path: str, csv_path: str) -> int:
    """
    log의 경로와 csv로 저장할 경로를 받아 .log (jsonlines) -> .csv 변환. \n
    반환 값: 변환된 레코드 수
    """
    count = 0
    with open(log_path, "r", encoding="utf-8") as fin, open(csv_path, "w", newline='', encoding="utf-8") as fout:
        reader = (json.loads(line) for line in fin)
        fieldnames = ["time", "action", "actor_name", "actor_email", "ip", "event_id"]
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        for obj in reader:
            # 보장되지 않은 키는 None으로 둠
            row = {k: obj.get(k) for k in fieldnames}
            writer.writerow(row)
            count += 1
    return count

# 백그라운드 작업: 실제 수집 로직
def collector_worker(filename: str, date_from: int, date_to: int, ui_callback_stop_animation):
    """
    감사 로그 수집 작업을 수행하는 백그라운드 워커 함수 \n
    ui_callback_stop_animation: 작업 완료 후 UI 애니메이션 중지 콜백
    """
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    uri = build_initial_uri()
    params = {"limit": PAGE_SIZE, "from": date_from, "to": date_to}
    log_path = os.path.join(LOGS_DIR, f"{filename}.log")
    total_count = 0

    ui_log_queue.put(f"시작: {datetime.now().isoformat()} | 파일: {log_path} | limit={PAGE_SIZE}")

    try:
        while True:
            resp_json = request_with_retries(uri, headers, params=params)
            events_data = extract_events_from_response(resp_json)
            ui_log_queue.put(f"수신: {len(events_data)} 이벤트")

            for item in events_data:
                rec = {
                    "time": item.get("attributes", {}).get("time"),
                    "action": item.get("attributes", {}).get("action"),
                    "actor_name": (item.get("attributes", {}).get("actor") or {}).get("name"),
                    "actor_email": (item.get("attributes", {}).get("actor") or {}).get("email"),
                    "ip": (item.get("attributes", {}).get("location") or {}).get("ip"),
                    "event_id": item.get("id")
                }
                append_jsonline(log_path, rec)
                total_count += 1

            next_token = get_next_cursor_from_response(resp_json)
            if not next_token:
                ui_log_queue.put("다음 토큰 없음: 수집 완료 조건 충족")
                break

            if isinstance(next_token, str) and next_token.startswith("http"):
                uri = next_token
                params = None
            else:
                uri = build_initial_uri()
                params = {"limit": PAGE_SIZE, "from": date_from, "to": date_to, "cursor": next_token}

        ui_log_queue.put(f"수집 완료: {total_count}개의 이벤트를 저장했습니다.")
        ui_log_queue.put(f"결과 파일: {log_path}")
        ui_callback_stop_animation(success=True, result_path=log_path)
    except Exception as e:
        ui_log_queue.put(f"[에러] 수집 중 예외 발생: {e}")
        ui_callback_stop_animation(success=False, result_path=None)

# UI: 팝업 창 + 로그 영역 + 애니메이션 + 결과 버튼들
class CollectorUI:
    """
    감사 로그 수집기 UI 클래스.\n
    root를 init의 인자로 받으며. tk.Tk 클래스의 인스턴스여야함.
    """
    def __init__(self, root : tk.Tk):
        self.root = root
        self.root.title("Audit Log Collector")
        self.root.geometry("700x520")
        self.root.resizable(False, False)

        # 입력 프레임
        in_frame = tk.Frame(root)
        in_frame.pack(fill="x", padx=10, pady=6)

        tk.Label(in_frame, text="파일명").grid(row=0, column=0, sticky="w")
        self.filename_entry = tk.Entry(in_frame, width=30)
        self.filename_entry.grid(row=0, column=1, padx=6, sticky="w")

        tk.Label(in_frame, text="시작 날짜 (YYYY-MM-DD)").grid(row=1, column=0, sticky="w")
        self.date_from_entry = tk.Entry(in_frame, width=20)
        self.date_from_entry.grid(row=1, column=1, padx=6, sticky="w")

        tk.Label(in_frame, text="종료 날짜 (YYYY-MM-DD)").grid(row=2, column=0, sticky="w")
        self.date_to_entry = tk.Entry(in_frame, width=20)
        self.date_to_entry.grid(row=2, column=1, padx=6, sticky="w")

        self.start_btn = tk.Button(in_frame, text="확인", command=self.on_start)
        self.start_btn.grid(row=0, column=2, rowspan=1, padx=10)

        # 애니메이션 / 상태 프레임
        status_frame = tk.Frame(root)
        status_frame.pack(fill="x", padx=10, pady=4)

        tk.Label(status_frame, text="상태:").pack(side="left")
        self.status_label = tk.Label(status_frame, text="대기중")
        self.status_label.pack(side="left", padx=(6, 20))

        self.progress = ttk.Progressbar(status_frame, mode="indeterminate", length=200)
        self.progress.pack(side="left")

        # 로그 영역
        log_frame = tk.Frame(root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)

        self.log_text = ScrolledText(log_frame, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

        # 완료 후 작업 버튼
        action_frame = tk.Frame(root)
        action_frame.pack(fill="x", padx=10, pady=8)

        self.open_folder_btn = tk.Button(action_frame, text="결과 폴더 열기", command=self.on_open_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=6)

        self.export_csv_btn = tk.Button(action_frame, text=".csv로 내보내기", command=self.on_export_csv, state="disabled")
        self.export_csv_btn.pack(side="left", padx=6)

        self.result_path_label = tk.Label(action_frame, text="")
        self.result_path_label.pack(side="left", padx=10)

        # UI 업데이터
        self.poll_ui_queue()

        # 내부 상태
        self.worker_thread = None
        self.current_log_path = None

    def poll_ui_queue(self):
        """
        200ms 마다 UI 로그 큐를 확인하여 로그 메시지를 가져와 로그 영역에 추가.
        Queue가 비어있다면 예외 발생 -> 무시하고 다시 스케줄링.
        """
        try:
            while True:
                msg = ui_log_queue.get_nowait()
                self.append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self.poll_ui_queue)

    def append_log(self, msg: str):
        """
        msg로 전달된 문자열을 log_text 영역에 추가 하는 메서드.\n
        - 각 로그 앞에 타임스탬프 추가.\n
        - 스크롤을 맨 아래로 이동.\n
        - 수정시에만 잠시 상태를 "normal"로 변경했다가 다시 "disabled"로 변경.\n
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def validate_dates(self, dfrom: str, dto: str):
        """
        날짜가 유효한 형태인지 검사하고 유효한 경우 밀리초(Atlassian Jira Cloud가 epoch 밀리초 단위를 사용하기 때문) 타임스탬프로 변환하여 반환.\n
        - 성공 반환 값: (from_timestamp_ms, to_timestamp_ms)
        - 실패 시 반환 값 : (None, None) (유효하지 않은 경우)\n
        09:00 KST 기준으로 날짜의 시작과 끝을 설정함.
        """
        KST = timezone(timedelta(hours=9))
        try:
            dt_from = datetime.strptime(dfrom, "%Y-%m-%d")
            dt_to = datetime.strptime(dto, "%Y-%m-%d")
            dt_from = dt_from.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=KST)
            dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=KST)
            return int(dt_from.timestamp() * 1000), int(dt_to.timestamp() * 1000)
        except ValueError:
            return None, None

    def on_start(self):
        """
        시작 버튼 클릭 핸들러.\n
        입력값 검증 후 UI 상태 변경 및 백그라운드 스레드 시작
        """
        filename = self.filename_entry.get().strip().removesuffix(".log")
        date_from_str = self.date_from_entry.get().strip()
        date_to_str = self.date_to_entry.get().strip()

        # 입력값이 체워져 있는지 검사
        if not filename or not date_from_str or not date_to_str:
            messagebox.showerror("입력 오류", "모든 항목을 입력해주세요.")
            return
        
        # 입력값 검증
        date_from, date_to = self.validate_dates(date_from_str, date_to_str)

        # 날짜 형식이 올바른지 검사
        if not date_from or not date_to:
            messagebox.showerror("날짜 형식 오류", "날짜는 YYYY-MM-DD 형식으로 입력해주세요.")
            return

        # UI 상태 변경
        self.start_btn.configure(state="disabled")
        self.status_label.configure(text="진행중")
        self.progress.start(100) # 0.1초 간격으로 애니메이션
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end") # 초기 로그 삭제
        self.log_text.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")
        self.export_csv_btn.configure(state="disabled")
        self.result_path_label.configure(text="")

        # 백그라운드 스레드 시작
        self.worker_thread = threading.Thread(
            target=collector_worker,
            args=(filename, date_from, date_to, self.ui_worker_done_callback),
            daemon=True
        )
        self.worker_thread.start()
        ui_log_queue.put("백그라운드 수집 작업이 시작되었습니다.")

    def ui_worker_done_callback(self, success: bool, result_path: str | None):
        # 이 콜백은 worker 스레드에서 직접 호출됨 -> UI 스레드에 안전하게 전달
        def _finish():
            self.progress.stop()
            self.status_label.configure(text="완료" if success else "실패")
            self.start_btn.configure(state="normal")
            if success and result_path:
                self.current_log_path = result_path
                folder = os.path.abspath(os.path.dirname(result_path))
                self.result_path_label.configure(text=folder)
                self.open_folder_btn.configure(state="normal")
                self.export_csv_btn.configure(state="normal")
            else:
                self.current_log_path = None
        # schedule on main thread
        self.root.after(100, _finish)

    def on_open_folder(self):
        """
        현재 log path가 설정되어 있을 때 해당 폴더를 탐색기로 여는 메서드.\n
        '폴더 경로 열기' 버튼 클릭의 핸들러
        """
        if not self.current_log_path:
            return
        # 폴더의(실재 파일 x) 절대 경로 가져오기
        folder = os.path.abspath(os.path.dirname(self.current_log_path))
        try:
            os.startfile(folder)
        except Exception as e:
            messagebox.showerror("폴더 열기 실패", str(e))

    def on_export_csv(self):
        """
        현재 log path가 설정되어 있을 때 .csv로 변환하는 메서드.\n
        '.csv로 내보내기' 버튼 클릭의 핸들러
        """
        if not self.current_log_path:
            return
        # 이름+확장자에서 이름만 분리해서 확장자를 .csv로 변경
        default_csv = os.path.splitext(os.path.basename(self.current_log_path))[0] + ".csv"
        default_folder = os.path.abspath(os.path.dirname(self.current_log_path))
        path = filedialog.asksaveasfilename(initialdir=default_folder,defaultextension=".csv", initialfile=default_csv, filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        try:
            cnt = convert_log_to_csv(self.current_log_path, path)
            messagebox.showinfo("내보내기 완료", f"{cnt}개의 레코드를 {path}에 저장했습니다.")
            ui_log_queue.put(f"CSV로 내보내기 완료: {path} (레코드 {cnt})")
        except Exception as e:
            messagebox.showerror("내보내기 실패", str(e))
            ui_log_queue.put(f"[에러] CSV 변환 실패: {e}")

def main():
    root = tk.Tk()
    app = CollectorUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()