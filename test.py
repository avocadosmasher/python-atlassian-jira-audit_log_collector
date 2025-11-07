from tkcalendar import DateEntry
from datetime import datetime
import tkinter as tk

def show_korean_date():
    d = cal.get_date()  # datetime.date 객체
    formatted = d.strftime("%YYYY년 %mm월 %dd일")
    print("선택한 날짜:", formatted)

root = tk.Tk()
cal = DateEntry(root, date_pattern="yyyy-mm-dd")
cal.pack(pady=10)

btn = tk.Button(root, text="날짜 확인", command=show_korean_date)
btn.pack()

root.mainloop()