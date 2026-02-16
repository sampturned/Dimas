from curl_cffi import requests
from bs4 import BeautifulSoup
import time
import re
from datetime import datetime
import threading
import random
import os
import json
import html
import urllib.parse
import urllib.request
from collections import deque
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Глобальные переменные
active_sabotage_threads = {}
active_sabotage_lock = threading.Lock()
spoiled_tasks_cache = {}
points_cache = {}
original_answers_cache = {}

active_solve_threads = {}
active_solve_lock = threading.Lock()

jobs_lock = threading.Lock()
active_jobs = {}
job_logs = {}
telegram_states = {}
job_counter = 0
_persist_thread_started = False
_persist_last_blob = None

HISTORY_FILE = "task_history.json"
LOGS_DIR = "job_logs"
ACCESS_FILE = "tg_access.json"
JOBS_FILE = "active_jobs.json"

# Данные для входа
LOGIN_URL = "https://www.yaklass.ru/Account/Login"
WORKS_URL = "https://www.yaklass.ru/testwork?p=1"
BASE_URL = "https://www.yaklass.ru"
USERNAME = "ekaterina.minina2012@yandex.ru"
PASSWORD = "191211"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8270054039:AAH24Ob5YpENH3172Oa61RbrCYQzo6cmH_Q").strip()
ADMIN_CHAT_ID = "7139672130"
ALLOWED_CHAT_IDS = {x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
approved_chat_ids = {ADMIN_CHAT_ID} | ALLOWED_CHAT_IDS
pending_access_requests = {}
SABOTAGE_NOTIFY_COOLDOWN = 0  # disabled: notify every time when percentage exceeded

headers = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

session = requests.Session()
session_auth_lock = threading.Lock()
YK_HTTP_TIMEOUT_SEC = 25

# Cache to avoid re-fetching large student lists repeatedly.
students_cache_lock = threading.Lock()
students_cache = {}  # key -> {"ts": float, "students": [(name,pct),...]}


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_runtime_files():
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump([], fh, ensure_ascii=False, indent=2)
    if not os.path.exists(ACCESS_FILE):
        with open(ACCESS_FILE, "w", encoding="utf-8") as fh:
            json.dump(
                {"approved_chat_ids": sorted(list(approved_chat_ids)), "pending": {}},
                fh,
                ensure_ascii=False,
                indent=2,
            )
    if not os.path.exists(JOBS_FILE):
        with open(JOBS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "saved_at": now_str(), "jobs": []}, fh, ensure_ascii=False, indent=2)


def _serialize_job_for_disk(job):
    """Return JSON-serializable subset of job dict. Excludes thread/event objects."""
    if not isinstance(job, dict):
        return None
    skip = {"thread", "stop_event"}
    out = {}
    for k, v in job.items():
        if k in skip:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
            continue
        if isinstance(v, (list, dict)):
            out[k] = v
            continue
        # Drop non-serializable runtime values.
    return out


def _snapshot_jobs_state_for_disk():
    """Build a deterministic snapshot for persistence."""
    with jobs_lock:
        jobs = []
        for job_id, job in sorted(active_jobs.items(), key=lambda x: int(x[0])):
            sj = _serialize_job_for_disk(job)
            if not sj:
                continue
            if str(sj.get("status", "running")) in ("stopped", "done"):
                continue
            jobs.append(sj)
        state = {
            "version": 1,
            "saved_at": now_str(),
            "job_counter": int(job_counter),
            "jobs": jobs,
        }
    return state


def persist_active_jobs_state(force=False):
    """Persist active_jobs to JOBS_FILE (atomic)."""
    global _persist_last_blob
    ensure_runtime_files()
    try:
        state = _snapshot_jobs_state_for_disk()
        blob = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if not force and _persist_last_blob == blob:
            return
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, JOBS_FILE)
        _persist_last_blob = blob
    except Exception:
        pass


def _persist_loop():
    while True:
        persist_active_jobs_state(force=False)
        time.sleep(3)


def ensure_persist_thread_started():
    global _persist_thread_started
    if _persist_thread_started:
        return
    _persist_thread_started = True
    threading.Thread(target=_persist_loop, daemon=True).start()


def restore_active_jobs_from_disk():
    """Restore active jobs after restart and restart their monitor threads."""
    global job_counter
    ensure_runtime_files()
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh) or {}
    except Exception:
        return 0

    jobs = state.get("jobs") or []
    restored = 0
    with jobs_lock:
        for j in jobs:
            try:
                jid = str(j.get("id", "")).strip()
                if not jid:
                    continue
                if jid in active_jobs:
                    continue

                stop_event = threading.Event()
                job = dict(j)
                job["id"] = jid
                job["status"] = "running"
                job["stop_event"] = stop_event
                job["alert_sent"] = False
                if not isinstance(job.get("work_state"), dict):
                    job["work_state"] = {}

                # Timestamps: normalize to floats where used as unix-ts.
                for k in (
                    "last_no_works_notice_ts",
                    "last_not_found_notice_ts",
                    "last_error_notice_ts",
                    "last_sabotage_notify_ts",
                ):
                    try:
                        job[k] = float(job.get(k) or 0.0)
                    except Exception:
                        job[k] = 0.0

                thread = threading.Thread(target=monitor_job_loop, args=(jid,), daemon=True)
                job["thread"] = thread
                active_jobs[jid] = job
                thread.start()
                restored += 1

                try:
                    job_counter = max(job_counter, int(jid))
                except Exception:
                    pass
            except Exception:
                continue

    if restored:
        append_history({"event": "restored", "count": restored, "saved_at": state.get("saved_at")})
    return restored


def init_job_counter_from_history():
    """Avoid reusing job ids after restart by restoring counter from HISTORY_FILE."""
    global job_counter
    ensure_runtime_files()
    max_id = 0
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh) or []
        for event in data:
            try:
                jid = int(str(event.get("job_id", "")).strip())
                if jid > max_id:
                    max_id = jid
            except Exception:
                continue
    except Exception:
        max_id = 0
    job_counter = max(job_counter, max_id)


def delete_history_for_job(job_id, requester_chat_id):
    """Remove all history events for given job_id. Non-admin can only delete their own jobs."""
    ensure_runtime_files()
    jid = str(job_id).strip()
    if not jid:
        return False, "Нужен id задачи."

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh) or []
    except Exception:
        return False, "История недоступна."

    admin = is_admin(requester_chat_id)
    req = str(requester_chat_id)

    if not admin:
        matches = [e for e in data if str(e.get("job_id", "")).strip() == jid]
        if not matches:
            return False, f"Записей для job={jid} нет."
        for e in matches:
            chat = e.get("chat_id")
            if not chat:
                return False, "Нельзя удалить: старая запись без chat_id (только админ)."
            if str(chat) != req:
                return False, "Нельзя удалить: задача не из этого чата."

    new_data = [e for e in data if str(e.get("job_id", "")).strip() != jid]
    removed = len(data) - len(new_data)
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(new_data[-500:], fh, ensure_ascii=False, indent=2)
    if removed <= 0:
        return False, f"Записей для job={jid} нет."
    return True, f"Удалено записей: {removed} (job={jid})."


def clear_history(requester_chat_id):
    """Clear entire history (admin only)."""
    if not is_admin(requester_chat_id):
        return False, "Команда доступна только администратору."
    ensure_runtime_files()
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump([], fh, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"Не удалось очистить историю: {e}"
    return True, "История очищена."

def load_access_state():
    global approved_chat_ids, pending_access_requests
    ensure_runtime_files()
    try:
        with open(ACCESS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    approved = set(str(x) for x in data.get("approved_chat_ids", []))
    approved.add(ADMIN_CHAT_ID)
    approved |= ALLOWED_CHAT_IDS
    approved_chat_ids = approved
    pending_access_requests = data.get("pending", {}) or {}


def save_access_state():
    ensure_runtime_files()
    with open(ACCESS_FILE, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "approved_chat_ids": sorted(list(approved_chat_ids)),
                "pending": pending_access_requests,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )


def is_admin(chat_id):
    return str(chat_id) == ADMIN_CHAT_ID


def append_history(event):
    ensure_runtime_files()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = []
    event["time"] = now_str()
    data.append(event)
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(data[-500:], fh, ensure_ascii=False, indent=2)


def job_log(job_id, message):
    ensure_runtime_files()
    line = f"[{now_str()}] {message}"
    with jobs_lock:
        if job_id not in job_logs:
            job_logs[job_id] = deque(maxlen=300)
        job_logs[job_id].append(line)

    path = os.path.join(LOGS_DIR, f"job_{job_id}.log")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    print(f"[Job {job_id}] {message}")


def notify_all_chats(text):
    if not approved_chat_ids:
        return
    for chat_id in sorted(approved_chat_ids):
        send_tg_message(chat_id, f"⚠️ {text}", parse_mode=None)


def notify_job(job_id, text, with_alert_icon=False):
    msg = f"⚠️ {text}" if with_alert_icon else text
    # Broadcast job notifications to all approved chats.
    recipients = set(str(x) for x in approved_chat_ids if str(x).strip())
    try:
        recipients.add(str(ADMIN_CHAT_ID))
    except Exception:
        pass
    if not recipients:
        return
    for cid in sorted(recipients):
        send_tg_message(cid, msg, parse_mode=None)


def should_send_periodic_notice(job_id, marker_key, cooldown_seconds):
    now_ts = time.time()
    with jobs_lock:
        job = active_jobs.get(str(job_id))
        if not job:
            return False
        last_ts = float(job.get(marker_key, 0.0) or 0.0)
        if now_ts - last_ts < cooldown_seconds:
            return False
        job[marker_key] = now_ts
        return True


def extract_ids_from_url(url):
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        test_result_id = query.get("testResultId", [None])[0]
        tw_id = query.get("twId", [None])[0]
        return test_result_id, tw_id
    except Exception:
        return None, None


def collect_overview_tasks(student_session, test_result_id, tw_id):
    if not test_result_id or not tw_id:
        return [], 0
    overview_url = f"{BASE_URL}/TestWorkRun/Overview?testResultId={test_result_id}&twId={tw_id}"
    try:
        resp = student_session.get(overview_url, headers=headers, impersonate="chrome120")
    except Exception as e:
        print(f"   [Sabotage] Не удалось открыть Overview: {e}")
        return [], 0

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="exercise-list-table")
    if not table:
        return [], 0

    tasks = []
    rows = table.find_all("tr")
    scored_tasks = 0
    for row in rows:
        link_tag = row.find("a", href=True)
        status_cell = row.find("td", class_="status")
        if not link_tag or not status_cell:
            continue
        status_text = status_cell.get_text(strip=True)
        if status_text == "Прочитано":
            continue
        href = link_tag["href"]
        full_link = href if href.startswith("http") else BASE_URL + href
        ex_pos = 0
        match = re.search(r"exercisePosition=(\d+)", href)
        if match:
            ex_pos = int(match.group(1))
        tasks.append((ex_pos, full_link))
        score_cell = row.find_all("td")[-1]
        if score_cell and score_cell.get_text(strip=True):
            scored_tasks += 1
    return tasks, scored_tasks


def extract_max_points(soup_details, cache_key=""):
    if cache_key and cache_key in points_cache:
        return points_cache[cache_key]

    max_points_map = {}
    total_max_points = 0.0

    info_rows = soup_details.find_all("tr", class_="info-row")
    for info_row in info_rows:
        cells = info_row.find_all(["td", "th"], class_="tooltip-styled")
        row_filled = False
        for idx, cell in enumerate(cells):
            bold = cell.find("b")
            if not bold:
                continue
            try:
                pts = float(bold.text.strip().replace(",", "."))
            except (ValueError, AttributeError):
                continue
            max_points_map[idx] = pts
            total_max_points += pts
            row_filled = True
        if row_filled:
            break

    if not max_points_map:
        header_row = soup_details.find("tr", class_="header-row")
        if header_row:
            ths = header_row.find_all("th", class_="tooltip-styled")
            for idx, th in enumerate(ths):
                title_attr = th.get("title", "")
                match = re.search(r"\((\d+([.,]\d+)?)<span", title_attr)
                if match:
                    try:
                        pts = float(match.group(1).replace(",", "."))
                    except ValueError:
                        continue
                    max_points_map[idx] = pts
                    total_max_points += pts

    result = (max_points_map, total_max_points)
    if cache_key:
        points_cache[cache_key] = result
    return result


def import_cookies_from_file(path="cookies.json"):
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cookies = json.load(fh)
        loaded = 0
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = cookie.get("domain")
            if not name or value is None:
                continue
            session.cookies.set(name, value, domain=domain, path=cookie.get("path", "/"))
            loaded += 1
        print(f"[Cookies] Импортировано {loaded} cookie из {path}")
        return loaded > 0
    except Exception as e:
        print(f"[Cookies] Не удалось загрузить {path}: {e}")
        return False


# === АВТОРИЗАЦИЯ ===
def login():
    print("--- Авторизация ---")
    for attempt in range(3):
        try:
            with session_auth_lock:
                resp_login_page = session.get(LOGIN_URL, headers=headers, impersonate="chrome120")
                print(f"[Debug] login GET status: {resp_login_page.status_code}")

                soup = BeautifulSoup(resp_login_page.text, "html.parser")
                token_input = soup.find("input", {"name": "PostToken"})

                if not token_input:
                    # Частый кейс: cookies.json уже дал валидную сессию, а страница логина не содержит PostToken.
                    if session.cookies.get(".AUTH") and "Account/Login" not in (resp_login_page.url or ""):
                        print("[Auth] Уже авторизованы по cookie (.AUTH).")
                        return True

                    try:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        with open(f"login_debug_{ts}.html", "w", encoding="utf-8") as fh:
                            fh.write(resp_login_page.text)
                        print(f"[Auth] PostToken не найден. Сохранил debug: login_debug_{ts}.html")
                    except Exception as dbg_err:
                        print(f"[Auth] PostToken не найден. Не удалось сохранить debug html: {dbg_err}")

                    print("Ошибка: Не найдено поле PostToken. Возможно, капча/блок или нестандартная страница.")
                    return False

                token = token_input.get("value")

                payload = {
                    "UserName": USERNAME,
                    "Password": PASSWORD,
                    "PostToken": token,
                    "AuthAction": "lor",
                    "ReturnUrl": "",
                }

                session.post(
                    LOGIN_URL,
                    data=payload,
                    headers=headers,
                    impersonate="chrome120",
                    allow_redirects=True,
                )
                if session.cookies.get(".AUTH"):
                    print("Успешно вошли!")
                    return True
                print("Не удалось получить cookie .AUTH")
        except Exception as e:
            print(f"Ошибка входа: {e}. Попытка {attempt+1}/3")
            time.sleep(2)
    return False


def periodic_relogin_loop(interval_seconds=1800):
    while True:
        time.sleep(interval_seconds)
        print("[Auth] Плановый перезаход...")
        ok = login()
        if ok:
            print("[Auth] Плановый перезаход успешен.")
        else:
            print("[Auth] Плановый перезаход не удался.")


# === СБРОС ВРЕМЕННОГО ПАРОЛЯ ===
def reset_one_time_password(student_session):
    reset_url = "https://www.yaklass.ru/Account/OneTimePasswordReset"
    print("   [Student] Проверка необходимости сброса временного пароля...")

    resp = student_session.get(reset_url, headers=headers, impersonate="chrome120")

    if "OneTimePasswordReset" in resp.url or "Смена пароля" in resp.text:
        print("   [Student] Требуется смена временного пароля. Меняем на '1234'...")
        payload = {"NewPassword": "1234", "ConfirmPassword": "1234"}
        resp_post = student_session.post(reset_url, data=payload, headers=headers, impersonate="chrome120")

        if resp_post.status_code == 200:
            print("   [Student] Временный пароль успешно сменен!")
            return True
        print("   [Student] Ошибка смены временного пароля.")
        return False

    print("   [Student] Сброс пароля не требуется (уже на главной).")
    return True


# === АВТОРИЗАЦИЯ ЗА УЧЕНИКА ===
def login_as_student(login_value, password):
    print(f"   [Student] Пробуем войти как ученик: {login_value} / {password}")
    student_session = requests.Session()
    try:
        resp_login_page = student_session.get(LOGIN_URL, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp_login_page.text, "html.parser")
        token = soup.find("input", {"name": "PostToken"}).get("value")

        payload = {
            "UserName": login_value,
            "Password": password,
            "PostToken": token,
            "AuthAction": "lor",
            "ReturnUrl": "",
        }

        resp_post = student_session.post(
            LOGIN_URL,
            data=payload,
            headers=headers,
            impersonate="chrome120",
            allow_redirects=True,
        )

        if "OneTimePasswordReset" in resp_post.url:
            reset_one_time_password(student_session)

        if student_session.cookies.get(".AUTH"):
            print("   [Student] Успешный вход!")
            return student_session
        print("   [Student] Ошибка входа (нет cookie .AUTH).")
        return None
    except Exception as e:
        print(f"   [Student] Ошибка: {e}")
        return None


# === ПОЛУЧЕНИЕ ЛОГИНА УЧЕНИКА ===
def get_student_login(user_id):
    edit_url = f"https://www.yaklass.ru/ManageSchool/EditUser/{user_id}"
    print(f"   [Info] Получаем логин для ID {user_id}...")
    try:
        resp = session.get(edit_url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp.text, "html.parser")

        login_div = soup.find("div", class_="form-control-static")
        if login_div and login_div.find("strong"):
            return login_div.find("strong").text.strip()

        email_input = soup.find("input", {"name": "Email"})
        if email_input:
            return email_input.get("value")
        return None
    except Exception as e:
        print(f"   [Info] Ошибка при получении логина: {e}")
        return None


def process_task_link(student_session, link, ex_pos, work_key=None):
    try:
        resp_task = student_session.get(link, headers=headers, impersonate="chrome120")
        soup_task = BeautifulSoup(resp_task.text, "html.parser")
        form = soup_task.find("form", class_="taskForm")
        if not form:
            return False

        has_cached_original = work_key and work_key in original_answers_cache and ex_pos in original_answers_cache[work_key]
        data = {}
        original_data = {}
        z99 = form.find("input", {"name": "____z99"})
        if z99:
            data["____z99"] = z99.get("value")

        inputs = form.find_all("input", {"type": "text"})
        radios = form.find_all("input", {"type": "radio"})
        checkboxes = form.find_all("input", {"type": "checkbox"})
        selects = form.find_all("select")

        dnd_inputs = []
        hidden_inputs = form.find_all("input", {"type": "hidden"})
        for h in hidden_inputs:
            if h.get("name", "").endswith("|dnd"):
                dnd_inputs.append(h)
        dnd_options = soup_task.find_all("div", class_="gxs-dnd-option")
        dnd_vals = [d.get("data-id") for d in dnd_options if d.get("data-id")]

        changed = 0

        for inp in inputs:
            name = inp.get("name")
            val = inp.get("value", "")
            if not name:
                continue
            original_data[name] = val
            new_val = val
            if val.isdigit():
                new_val = str(int(val) + 1)
            elif val:
                if len(val) > 1:
                    pos = random.randint(0, len(val) - 1)
                    new_val = val[:pos] + val[pos] + val[pos:]
                else:
                    new_val = val + val
            else:
                continue
            data[name] = new_val
            changed += 1

        radio_groups = {}
        for r in radios:
            n = r.get("name")
            if n:
                radio_groups.setdefault(n, []).append(r)

        for n, group in radio_groups.items():
            curr = None
            vals = []
            for r in group:
                v = r.get("value")
                vals.append(v)
                if r.has_attr("checked"):
                    curr = v
            if not vals:
                continue
            original_data[n] = curr
            new_v = next((v for v in vals if v != curr), vals[0])
            data[n] = new_v
            changed += 1

        checkbox_groups = {}
        for cb in checkboxes:
            n = cb.get("name")
            if not n:
                continue
            group_key = n.split("|", 1)[0] if "|" in n else n
            checkbox_groups.setdefault(group_key, []).append(cb)

        for _, group_boxes in checkbox_groups.items():
            unchecked = [cb for cb in group_boxes if not cb.has_attr("checked")]
            if not unchecked:
                continue
            cb = unchecked[0]
            val = cb.get("value") or "on"
            data[cb.get("name")] = val
            changed += 1

        for sel in selects:
            n = sel.get("name")
            if not n:
                continue
            opts = [o for o in sel.find_all("option") if o.get("value")]
            if not opts:
                continue
            sel_opt = sel.find("option", selected=True)
            curr = sel_opt.get("value") if sel_opt else ""
            original_data[n] = curr
            new_v = next((o.get("value") for o in opts if o.get("value") != curr), opts[0].get("value"))
            data[n] = new_v
            changed += 1

        # MathML/графические инпуты (gxst-formula-box) с атрибутом data-name.
        math_inputs = soup_task.find_all(attrs={"data-name": True})
        for mi in math_inputs:
            n = mi.get("data-name")
            if not n or n in data:
                continue
            orig_val = mi.get("data-value") or mi.get("value") or mi.get_text(strip=True)
            original_data[n] = orig_val
            new_v = orig_val or ""
            if orig_val:
                num_match = re.fullmatch(r"-?\d+[.,]?\d*", orig_val.strip())
                if num_match:
                    try:
                        num = float(orig_val.replace(",", "."))
                        num += 1
                        new_v = f"{num}".replace(".", ",") if "," in orig_val else str(num)
                    except Exception:
                        new_v = orig_val + orig_val[-1]
                elif len(orig_val) > 1:
                    pos = random.randint(0, len(orig_val) - 1)
                    new_v = orig_val[:pos] + orig_val[pos] + orig_val[pos:]
                else:
                    new_v = orig_val + orig_val
            else:
                new_v = "1"
            data[n] = new_v
            changed += 1

        for dnd in dnd_inputs:
            n = dnd.get("name")
            if dnd_vals:
                data[n] = dnd_vals[-1]
                changed += 1

        if changed == 0:
            print(f"      -> [Skip] Задание {ex_pos}: нет изменений для порчи")
            return False

        if has_cached_original:
            cached_original = original_answers_cache[work_key][ex_pos]
            answers_match_original = True
            for key, orig_val in cached_original.items():
                if key in original_data and str(original_data[key]) != str(orig_val):
                    answers_match_original = False
                    break
            if not answers_match_original:
                print(f"      -> [Skip] Задание {ex_pos}: уже испорчено, ученик не изменил ответ")
                return False
            print(f"      -> [Detect] Задание {ex_pos}: ученик вернул оригинальный ответ!")

        if work_key and original_data:
            if work_key not in original_answers_cache:
                original_answers_cache[work_key] = {}
            if ex_pos not in original_answers_cache[work_key]:
                original_answers_cache[work_key][ex_pos] = original_data
                print(f"      -> [Cache] Сохранены оригинальные ответы для задания {ex_pos}")
            else:
                print("      -> [Cache] Используем ранее сохраненный оригинал")

        data["answerAction"] = "correct"
        post_url = BASE_URL + form.get("action")
        resp_save = student_session.post(post_url, data=data, headers=headers, impersonate="chrome120")
        if resp_save.status_code == 200:
            print(f"      -> [OK] Ответ испорчен (задача {ex_pos}).")
            return True
        print(f"      -> [Error] Не удалось испортить задание {ex_pos} (status: {resp_save.status_code})")
        return False
    except Exception as e:
        print(f"   [Sabotage] Ошибка задания {ex_pos}: {e}")
    return False


# === ПОЛУЧЕНИЕ ПРАВИЛЬНЫХ ОТВЕТОВ (teacher ExerciseResult) ===
def normalize_text(t):
    return re.sub(r"\s+", " ", (t or "").strip()).lower()


def _looks_numeric(s):
    s = (s or "").strip()
    if not s:
        return False
    # allow minus, comma/dot decimal, and simple fractions like 1/2
    return bool(re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?(?:\s*/\s*[-+]?\d+(?:[.,]\d+)?)?", s))


def extract_correct_texts(teacher_soup):
    # 1) Пытаемся брать только отмеченные как правильные (li.checked)
    checked = []
    for li in teacher_soup.select("ul.correct-answer li.checked, ul.correct-answer li[data-is-correct='true']"):
        txt = li.get_text(" ", strip=True)
        if txt:
            checked.append(txt)
    for span in teacher_soup.select("span.correct-answer.checked, span.correct-answer[data-is-correct='true']"):
        txt = span.get_text(" ", strip=True)
        if txt:
            checked.append(txt)
    for elem in teacher_soup.select(".correct-answer .checked [data-value], .correct-answer [data-is-correct='true'][data-value]"):
        val = elem.get("data-value")
        if val:
            checked.append(str(val))
    if checked:
        return checked

    texts = []
    for li in teacher_soup.select("ul.correct-answer li"):
        txt = li.get_text(" ", strip=True)
        if txt:
            texts.append(txt)
    for span in teacher_soup.select("span.correct-answer"):
        txt = span.get_text(" ", strip=True)
        if txt:
            texts.append(txt)
    for elem in teacher_soup.select(".correct-answer [data-value]"):
        val = elem.get("data-value")
        if val:
            texts.append(str(val))
    # plain numeric answers
    for span in teacher_soup.select(".gxs-result-number .correct-answer"):
        txt = span.get_text(" ", strip=True)
        if txt:
            texts.append(txt)
    return texts


def extract_correct_indices(teacher_soup):
    idxs = []
    for i, li in enumerate(teacher_soup.select("ul.correct-answer li")):
        cls = " ".join(li.get("class", []))
        if "checked" in cls or li.get("data-is-correct") == "true":
            idxs.append(i)
    return idxs


def extract_select_correct_indices(teacher_soup):
    """For each select block (single/multiple) return list of correct option indexes within that block."""
    blocks = []
    for blk in teacher_soup.select(".gxs-result.gxs-result-select"):
        ul = blk.select_one("ul.correct-answer")
        if not ul:
            continue
        idxs = []
        for i, li in enumerate(ul.select("li")):
            cls = " ".join(li.get("class", []))
            if "checked" in cls or li.get("data-is-correct") == "true":
                idxs.append(i)
        blocks.append(idxs)
    return blocks


def extract_text_correct_values(teacher_soup):
    """Collect correct answers for text-entry blocks in teacher result order."""
    values = []
    for span in teacher_soup.select(".gxs-result.gxs-result-text .correct-answer"):
        txt = span.get_text(" ", strip=True)
        if txt:
            values.append(txt)
    return values


def extract_dropdown_correct_values(teacher_soup):
    """Collect correct values for dropdown blocks in teacher result order."""
    values = []
    for span in teacher_soup.select(".gxs-result.gxs-result-dropdown .correct-answer"):
        # Keep empty entries too: some punctuation tasks have a valid "no symbol" answer,
        # and skipping blanks breaks per-dropdown positional mapping.
        txt = span.get_text(" ", strip=True)
        values.append(txt)
    return values


def extract_select_correct_values(teacher_soup):
    """Collect correct labels for select(single/multiple) blocks in teacher result order."""
    values = []
    for blk in teacher_soup.select(".gxs-result.gxs-result-select"):
        ul = blk.select_one("ul.correct-answer")
        if not ul:
            continue
        selected_text = ""
        for li in ul.select("li"):
            cls = " ".join(li.get("class", []))
            if "checked" in cls or li.get("data-is-correct") == "true":
                selected_text = li.get_text(" ", strip=True)
                break
        values.append(selected_text)
    return values


def extract_named_values(teacher_soup):
    """Map templated inputs (data-name) to their correct data-value from teacher page."""
    mapping = {}
    for el in teacher_soup.select(".correct-answer [data-name][data-value]"):
        name = el.get("data-name")
        val = el.get("data-value")
        if name and val is not None:
            mapping[name] = str(val)
    return mapping


def extract_dnd_mapping(teacher_soup):
    """Return mapping from slot order to correct option data-id for DnD."""
    mapping = []
    for res in teacher_soup.select(".gxs-result.gxs-result-dnd"):
        correct = res.select_one(".correct-answer .gxs-dnd-option")
        if correct and correct.get("data-id"):
            mapping.append(correct.get("data-id"))
    return mapping


def build_solution_payload(student_soup, teacher_texts, work_key=None):
    form = student_soup.find("form", class_="taskForm")
    if not form:
        return None
    data = {}

    def put_value(name, value, multi=False):
        if not name:
            return
        if not multi:
            data[name] = value
            return
        if name not in data:
            data[name] = [value]
            return
        if isinstance(data[name], list):
            data[name].append(value)
        else:
            data[name] = [data[name], value]
    z99 = form.find("input", {"name": "____z99"})
    if z99:
        data["____z99"] = z99.get("value")

    # Important: include hidden inputs. Yaklass often uses hidden fields to represent "unchecked" state
    # (or carry required metadata). Without them, checkbox answers can become "sticky" and never reset.
    for h in form.find_all("input", {"type": "hidden"}):
        n = h.get("name")
        if not n or n in data:
            continue
        v = h.get("value")
        if v is None:
            continue
        put_value(n, v)

    tnorm = [normalize_text(t) for t in teacher_texts]
    numeric_texts = [t.strip() for t in teacher_texts if _looks_numeric(t)]
    named_vals = getattr(student_soup, "_teacher_named_values", {}) or {}
    teacher_text_values = getattr(student_soup, "_teacher_text_values", []) or []
    teacher_dropdown_values = getattr(student_soup, "_teacher_dropdown_values", []) or []
    teacher_select_values = getattr(student_soup, "_teacher_select_values", []) or []

    def _is_dropdown_like_select(opts):
        if not opts:
            return False
        raw_texts = [(o.get_text(" ", strip=True) or "").strip() for o in opts]
        non_empty = [t for t in raw_texts if t]
        if not non_empty:
            return True
        punct = set(",.;:!?-—()[]{}«»\"'…")
        for t in non_empty:
            tt = t.replace(" ", "")
            if len(tt) > 3:
                return False
            if any(ch not in punct for ch in tt):
                return False
        return True
    # radios
    select_correct = getattr(student_soup, "_select_correct_indices", []) or []
    radio_groups = {}
    radio_order = []
    for r in form.find_all("input", {"type": "radio"}):
        n = r.get("name")
        if n:
            if n not in radio_groups:
                radio_order.append(n)
            radio_groups.setdefault(n, []).append(r)
    for gi, n in enumerate(radio_order):
        group = radio_groups.get(n) or []
        best = None
        # Prefer per-block correct indexes from teacher page. Text matching is global and
        # fails for repeated labels like "Верно/Неверно" across multiple blocks.
        if gi < len(select_correct) and select_correct[gi]:
            ci = select_correct[gi][0]
            if 0 <= ci < len(group):
                best = group[ci].get("value")
        for r in group:
            label = form.find("label", {"for": r.get("id")})
            ltxt = ""
            if label:
                ltxt = normalize_text(label.get_text(" ", strip=True))
            if not best and tnorm and ltxt in tnorm:
                best = r.get("value")
                break
        if not best and group:
            best = group[0].get("value")
        if best:
            put_value(n, best)

    # checkboxes (multiple choice)
    checkbox_groups = {}
    checkbox_order = []
    for cb in form.find_all("input", {"type": "checkbox"}):
        n = cb.get("name")
        if not n:
            continue
        key = n.split("|", 1)[0] if "|" in n else n
        if key not in checkbox_groups:
            checkbox_order.append(key)
        checkbox_groups.setdefault(key, []).append(cb)
    # checkbox blocks идут после radio-блоков в extract_select_correct_indices, поэтому смещаем индекс
    cb_base = len(radio_order)
    for gi, key in enumerate(checkbox_order):
        group = checkbox_groups.get(key) or []
        chosen = set()
        if (cb_base + gi) < len(select_correct):
            chosen = set(select_correct[cb_base + gi] or [])
        if chosen:
            for idx, cb in enumerate(group):
                if idx in chosen:
                    put_value(cb.get("name"), cb.get("value") or "on", multi=True)
        else:
            for cb in group:
                label = form.find("label", {"for": cb.get("id")})
                ltxt = normalize_text(label.get_text(" ", strip=True)) if label else ""
                if tnorm and ltxt in tnorm:
                    put_value(cb.get("name"), cb.get("value") or "on", multi=True)
            if not tnorm and group:
                put_value(group[0].get("name"), group[0].get("value") or "on", multi=True)

    # selects
    select_inputs = form.find_all("select")
    dropdown_idx = 0
    select_idx = 0
    for sidx, sel in enumerate(select_inputs):
        n = sel.get("name")
        if not n:
            continue
        opts = sel.find_all("option")
        picked = None
        dropdown_like = _is_dropdown_like_select(opts)

        # Prefer per-select dropdown answers from teacher page for mixed text+dropdown tasks.
        if dropdown_like and dropdown_idx < len(teacher_dropdown_values):
            raw_target = teacher_dropdown_values[dropdown_idx]
            dropdown_idx += 1
            target = normalize_text(raw_target)
            if target:
                for o in opts:
                    txt = normalize_text(o.get_text(" ", strip=True))
                    if txt == target:
                        picked = o.get("value")
                        break
            else:
                # Explicitly handle "empty symbol" answers (no comma/dash, etc.).
                for o in opts:
                    o_txt = (o.get_text(" ", strip=True) or "").strip()
                    o_val = (o.get("value") or "").strip()
                    if not o_txt or not o_val:
                        picked = o.get("value")
                        break

        # For regular select tasks, prefer ordered teacher select labels.
        if not picked and (not dropdown_like) and select_idx < len(teacher_select_values):
            raw_target = teacher_select_values[select_idx]
            select_idx += 1
            target = normalize_text(raw_target)
            if target:
                for o in opts:
                    txt = normalize_text(o.get_text(" ", strip=True))
                    if txt == target:
                        picked = o.get("value")
                        break

        if not picked:
            for o in opts:
                txt = normalize_text(o.get_text(" ", strip=True))
                if tnorm and txt in tnorm:
                    picked = o.get("value")
                    break
        if not picked and opts:
            picked = opts[0].get("value")
        if picked:
            put_value(n, picked)

    # text inputs (plain) — заполняем по порядку ответами учителя
    text_inputs = form.find_all("input", {"type": "text"})
    if text_inputs:
        for idx, inp in enumerate(text_inputs):
            n = inp.get("name")
            if not n:
                continue
            if n in named_vals:
                put_value(n, named_vals[n])
                continue

            # For mixed tasks (select + text), use dedicated text-block answers first,
            # otherwise global teacher_texts may contain select labels/explanations.
            src = teacher_text_values or numeric_texts or teacher_texts
            if not src:
                continue
            ans = str(src[idx % len(src)]).strip()
            if ans:
                put_value(n, ans)

    # formula boxes data-name
    formula_inputs = [mi for mi in student_soup.find_all(attrs={"data-name": True}) if mi.get("data-name")]
    for idx, mi in enumerate(formula_inputs):
        n = mi.get("data-name")
        if n in data:
            continue
        if n in named_vals:
            put_value(n, named_vals[n])
        elif teacher_texts:
            src = numeric_texts or teacher_texts
            put_value(n, src[idx % len(src)])
        else:
            put_value(n, "1")

    # dnd: map from teacher correct ids if available
    dnd_inputs = []
    hidden_inputs = form.find_all("input", {"type": "hidden"})
    dnd_vals = [d.get("data-id") for d in student_soup.find_all("div", class_="gxs-dnd-option") if d.get("data-id")]
    for h in hidden_inputs:
        if h.get("name", "").endswith("|dnd"):
            dnd_inputs.append(h)
    teacher_dnd = student_soup._teacher_dnd_ids if hasattr(student_soup, "_teacher_dnd_ids") else []
    for idx, dnd in enumerate(dnd_inputs):
        if teacher_dnd and idx < len(teacher_dnd):
            put_value(dnd.get("name"), teacher_dnd[idx])
        elif dnd_vals:
            put_value(dnd.get("name"), dnd_vals[-1])

    if not data:
        return None
    data["answerAction"] = "correct"
    return data


def solve_task(student_session, test_result_id, tw_id, ex_pos, work_key=None):
    teacher_url = f"{BASE_URL}/TestWork/ExerciseResult?testResultId={test_result_id}&exercisePosition={ex_pos}&twId={tw_id}&sendContentLog=True"
    stud_url = f"{BASE_URL}/TestWorkRun/Exercise?testResultId={test_result_id}&exercisePosition={ex_pos}&twId={tw_id}"
    try:
        resp_teacher = session.get(teacher_url, headers=headers, impersonate="chrome120")
        teacher_soup = BeautifulSoup(resp_teacher.text, "html.parser")
        correct_texts = extract_correct_texts(teacher_soup)
        select_correct = extract_select_correct_indices(teacher_soup)
        named_vals = extract_named_values(teacher_soup)
        teacher_dnd = extract_dnd_mapping(teacher_soup)
        text_values = extract_text_correct_values(teacher_soup)
        dropdown_values = extract_dropdown_correct_values(teacher_soup)
        select_values = extract_select_correct_values(teacher_soup)

        resp_stud = student_session.get(stud_url, headers=headers, impersonate="chrome120")
        student_soup = BeautifulSoup(resp_stud.text, "html.parser")
        student_soup._teacher_dnd_ids = teacher_dnd  # pass to payload builder
        student_soup._select_correct_indices = select_correct
        student_soup._teacher_named_values = named_vals
        student_soup._teacher_text_values = text_values
        student_soup._teacher_dropdown_values = dropdown_values
        student_soup._teacher_select_values = select_values
        # Save remaining time if present
        try:
            timer_div = student_soup.find("div", class_="tst-time")
            if timer_div and timer_div.get("data-left-time"):
                student_soup._left_time = int(timer_div.get("data-left-time"))
        except Exception:
            pass
        form = student_soup.find("form", class_="taskForm")
        if not form:
            return False
        payload = build_solution_payload(student_soup, correct_texts, work_key=work_key)
        if not payload:
            return False
        post_url = BASE_URL + form.get("action")
        resp_save = student_session.post(post_url, data=payload, headers=headers, impersonate="chrome120")
        return resp_save.status_code == 200
    except Exception as e:
        print(f"[Solve] Ошибка задания {ex_pos}: {e}")
        return False


def read_left_time(session_obj, url):
    try:
        resp = session_obj.get(url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp.text, "html.parser")
        timer_div = soup.find("div", class_="tst-time")
        if timer_div and timer_div.get("data-left-time"):
            return int(timer_div.get("data-left-time"))
    except Exception:
        return None
    return None

# === ИЗМЕНЕНИЕ ОТВЕТОВ ===
def sabotage_answers(student_session, current_url, work_key, target_percentage, max_work_points=0, tasks_to_spoil=None):
    if tasks_to_spoil is None:
        tasks_to_spoil = []

    print("\n   [Sabotage] ЗАПУСК ПРОТОКОЛА 'SABOTAGE'...")

    test_result_id, tw_id = extract_ids_from_url(current_url)

    try:
        resp = student_session.get(current_url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp.text, "html.parser")
        finish_screen = bool(soup.find(id="finishTestBtn") or soup.find("div", class_="answer-result-header"))

        if finish_screen:
            print("   [Sabotage] Обнаружен финальный экран. Переходим к Overview для обработки заданий...")
            if not test_result_id or not tw_id:
                print("   [Sabotage] Нет test_result_id или tw_id для перехода к Overview.")
                return
            overview_tasks, scored_tasks = collect_overview_tasks(student_session, test_result_id, tw_id)
            print(f"   [Sabotage] Overview вернул {len(overview_tasks)} заданий: {[t[0] for t in overview_tasks]}")

            if tasks_to_spoil:
                overview_tasks = [t for t in overview_tasks if t[0] in tasks_to_spoil]
                print(f"   [Sabotage] После фильтрации по списку {tasks_to_spoil}: {len(overview_tasks)} заданий")

            print(f"   [Sabotage] Найдено {len(overview_tasks)} заданий для обработки (scored: {scored_tasks})")
            for ex_pos, link in overview_tasks:
                print(f"      -> [Process] Обработка задания {ex_pos}...")
                process_task_link(student_session, link, ex_pos, work_key)
                time.sleep(1)

            if scored_tasks <= 2:
                print(f"   [Sabotage] Обнаружено {scored_tasks} задание(й). Автоматическое завершение работы...")
                complete_url = f"{BASE_URL}/TestWorkRun/CompleteTest"
                payload = {"testResultId": test_result_id, "twId": tw_id}
                resp_complete = student_session.post(complete_url, data=payload, headers=headers, impersonate="chrome120")
                if resp_complete.status_code == 200:
                    print("   [Sabotage] Работа автоматически завершена через CompleteTest.")
                else:
                    print(f"   [Sabotage] Не удалось завершить попытку ({resp_complete.status_code}).")
            return

        continue_btn = soup.find("button", id="continueWorkBtn")
        if continue_btn and continue_btn.get("data-next"):
            next_url = continue_btn.get("data-next")
            next_full = next_url if next_url.startswith("http") else BASE_URL + next_url
            print("   [Sabotage] Обнаружена кнопка 'Продолжить'. Переходим к следующему заданию.")
            return sabotage_answers(student_session, next_full, work_key, target_percentage, max_work_points, tasks_to_spoil)

        if tasks_to_spoil and test_result_id and tw_id:
            print(f"   [Sabotage] Есть список заданий {tasks_to_spoil}. Пробуем Overview режим...")
            overview_tasks, scored_tasks = collect_overview_tasks(student_session, test_result_id, tw_id)

            if overview_tasks:
                overview_tasks = [t for t in overview_tasks if t[0] in tasks_to_spoil]
                print(f"   [Sabotage] Найдено {len(overview_tasks)} заданий для обработки")
                for ex_pos, link in overview_tasks:
                    process_task_link(student_session, link, ex_pos, work_key)
                    time.sleep(1)

                if scored_tasks <= 2:
                    print(f"   [Sabotage] Обнаружено {scored_tasks} задание(й). Автоматическое завершение работы...")
                    complete_url = f"{BASE_URL}/TestWorkRun/CompleteTest"
                    payload = {"testResultId": test_result_id, "twId": tw_id}
                    resp_complete = student_session.post(complete_url, data=payload, headers=headers, impersonate="chrome120")
                    if resp_complete.status_code == 200:
                        print("   [Sabotage] Работа автоматически завершена через CompleteTest.")
                    else:
                        print(f"   [Sabotage] Не удалось завершить попытку ({resp_complete.status_code}).")
                return

            print("   [Sabotage] Overview недоступен. Используем альтернативный метод...")

        task_queue = []
        if tasks_to_spoil:
            parsed = urlparse(current_url)
            for ex_pos in tasks_to_spoil:
                query = parse_qs(parsed.query)
                query["exercisePosition"] = [str(ex_pos)]
                new_query = urlencode(query, doseq=True)
                new_link = urlunparse(parsed._replace(query=new_query))
                task_queue.append((ex_pos, new_link))
        else:
            nav_list = soup.find("ul", class_="ex-nav-list")
            if not nav_list:
                print("   [Sabotage] Не найдена навигация!")
            else:
                items = nav_list.find_all("li")
                for item in items:
                    classes = item.get("class", [])
                    if "answered" in classes:
                        link_tag = item.find("a")
                        full_link = ""
                        if link_tag:
                            full_link = BASE_URL + link_tag.get("href")
                        elif "current" in classes:
                            full_link = current_url
                        if full_link:
                            ex_pos = 0
                            match = re.search(r"exercisePosition=(\d+)", full_link)
                            if match:
                                ex_pos = int(match.group(1))
                            task_queue.append((ex_pos, full_link))

        if finish_screen and task_queue:
            print("   [Sabotage] Итоговый экран — переходим к Overview и игнорируем локальную навигацию.")
            task_queue = []

        overview_mode = False
        scored_tasks = 0
        if not task_queue:
            overview_tasks, scored_tasks = collect_overview_tasks(student_session, test_result_id, tw_id)
            if tasks_to_spoil:
                overview_tasks = [t for t in overview_tasks if t[0] in tasks_to_spoil]
            for ex_pos, link in overview_tasks:
                task_queue.append((ex_pos, link))
            if task_queue:
                overview_mode = True

        if overview_mode and scored_tasks <= 2 and test_result_id and tw_id:
            print(f"   [Sabotage] Обнаружено {scored_tasks} задание(й). Автоматическое завершение работы...")
            complete_url = f"{BASE_URL}/TestWorkRun/CompleteTest"
            payload = {"testResultId": test_result_id, "twId": tw_id}
            resp_complete = student_session.post(complete_url, data=payload, headers=headers, impersonate="chrome120")
            if resp_complete.status_code == 200:
                print("   [Sabotage] Работа автоматически завершена через CompleteTest.")
            else:
                print(f"   [Sabotage] Не удалось завершить попытку ({resp_complete.status_code}).")
            return

        if overview_mode:
            print(f"   [Sabotage] Overview режим: {len(task_queue)} заданий")
            for ex_pos, link in task_queue:
                process_task_link(student_session, link, ex_pos, work_key)
                time.sleep(1)
            return

        print(f"   [Sabotage] План к изменению: {len(task_queue)} заданий")
        if not task_queue:
            print("   [Sabotage] Нет доступных заданий для изменения.")
            return

        for ex_pos, link in task_queue:
            process_task_link(student_session, link, ex_pos, work_key)
            time.sleep(1)

    except Exception as e:
        print(f"   [Sabotage] Ошибка: {e}")


# === ВХОД В РАБОТУ ===
def enter_student_work(student_session, work_title, work_key, target_percentage, tasks_to_spoil, test_result_id=None, tw_id=None):
    print(f"   [StudentWork] Ищем работу '{work_title}'...")
    student_works_url = "https://www.yaklass.ru/testwork?from=menu"
    if test_result_id:
        base_url = f"{BASE_URL}/TestWorkRun/Exercise?testResultId={test_result_id}&exercisePosition=1"
        if tw_id:
            base_url += f"&twId={tw_id}"
        sabotage_answers(student_session, base_url, work_key, target_percentage, 0, tasks_to_spoil)
        return

    try:
        resp = student_session.get(student_works_url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("tr", itemprop="itemListElement")
        target_link = None

        for row in rows:
            title_cell = row.find("td", class_="testwork")
            if title_cell:
                link_tag = title_cell.find("a")
                if link_tag:
                    title = link_tag.text.strip()
                    if work_title.lower() in title.lower() or title.lower() in work_title.lower():
                        target_link = link_tag.get("href")
                        break

        if not target_link:
            print("   [StudentWork] Работа не найдена!")
            return

        preview_url = BASE_URL + target_link
        resp_preview = student_session.get(preview_url, headers=headers, impersonate="chrome120")
        max_work_points = 0

        soup_preview = BeautifulSoup(resp_preview.text, "html.parser")
        form = soup_preview.find("form", action=re.compile(r"/TestWorkRun/Start/"))

        if not form:
            if "/Exercise" in resp_preview.url:
                sabotage_answers(student_session, resp_preview.url, work_key, target_percentage, max_work_points, tasks_to_spoil)
                return
            print("   [StudentWork] Кнопка 'Продолжить' не найдена.")
            return

        start_url = BASE_URL + form.get("action")
        resp_start = student_session.post(start_url, headers=headers, impersonate="chrome120", allow_redirects=True)

        if "Exercise" in resp_start.url:
            sabotage_answers(student_session, resp_start.url, work_key, target_percentage, max_work_points, tasks_to_spoil)

    except Exception as e:
        print(f"   [StudentWork] Ошибка: {e}")


# === ОРКЕСТРАТОР ===
def force_set_student_password(user_id, new_password="1234"):
    """Set student password from teacher account. Returns True on HTTP 200."""
    change_pw_url = f"https://www.yaklass.ru/ManageSchool/ChangePassword/{user_id}"
    try:
        session.get(change_pw_url, headers=headers, impersonate="chrome120")
        payload = {
            "NewPassword": new_password,
            "ConfirmPassword": new_password,
            "Id": user_id,
            "ReturnUrl": "/ManageSchool/Users",
        }
        resp_post = session.post(change_pw_url, data=payload, headers=headers, impersonate="chrome120")
        return resp_post.status_code == 200
    except Exception:
        return False


def change_user_password_and_login(user_id, user_name, work_title, work_key, target_percentage, tasks_to_spoil, test_result_id=None, tw_id=None):
    student_login = get_student_login(user_id)
    if not student_login:
        return

    print(f"   [Password] Смена пароля для {user_name}...")

    try:
        if force_set_student_password(user_id, "1234"):
            print("   [Password] Пароль изменен.")
            student_session = login_as_student(student_login, "1234")
            if student_session:
                enter_student_work(
                    student_session,
                    work_title,
                    work_key,
                    target_percentage,
                    tasks_to_spoil,
                    test_result_id,
                    tw_id,
                )
    except Exception as e:
        print(f"   [Password] Ошибка: {e}")


def select_teacher():
    filter_url = "https://www.yaklass.ru/testwork?p=1&t="
    try:
        resp = session.get(filter_url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp.text, "html.parser")
        select_elem = soup.find("select", {"id": "FilterTeacher"})
        if not select_elem:
            return [""]

        options = select_elem.find_all("option")
        teachers = []
        print("\nДоступные учителя:")
        for idx, opt in enumerate(options):
            t_id = opt.get("value")
            t_name = opt.text.strip()
            teachers.append((t_id, t_name))
            print(f"{idx}. {t_name}")

        choice = input("\nВыберите номера учителей (через запятую, Enter для всех): ").strip()
        if not choice:
            return [""]

        selected = []
        for part in choice.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part)
                if 0 <= idx < len(teachers):
                    selected.append(teachers[idx][0])
            except ValueError:
                continue
        return selected or [""]
    except Exception:
        return [""]


def worker_sabotage(
    user_id,
    student_name_in_row,
    work_title,
    work_key,
    target_percentage,
    tasks_to_spoil,
    test_result_id=None,
    tw_id=None,
    job_id=None,
):
    print(f"\n[Thread] Поток для: {student_name_in_row}. Цели: {tasks_to_spoil}")
    try:
        if job_id:
            notify_job(job_id, f"Запущена обработка работы '{work_title}' ({student_name_in_row}).")
        change_user_password_and_login(
            user_id,
            student_name_in_row,
            work_title,
            work_key,
            target_percentage,
            tasks_to_spoil,
            test_result_id,
            tw_id,
        )
        if job_id:
            notify_job(job_id, f"Обработка работы '{work_title}' завершена.")
    except Exception as e:
        if job_id:
            notify_job(job_id, f"Ошибка обработки '{work_title}': {e}", with_alert_icon=True)
        print(f"[Thread] Ошибка в worker_sabotage: {e}")
    finally:
        with active_sabotage_lock:
            if work_key in active_sabotage_threads:
                del active_sabotage_threads[work_key]


def worker_solve(job, work, result, work_key, job_id=None):
    """Solve mode: пытаемся решить задания, затем завершить попытку с задержкой."""
    try:
        jid = job_id or job.get("id")
        pct_min = float(job.get("pct_min", 0) or 0)
        pct_max = float(job.get("pct_max", 0) or 100)
        delay_min = int(job.get("delay_min_sec", 60) or 60)
        delay_max = int(job.get("delay_max_sec", 180) or 180)
        cur_pct = float(result.get("current_pct", 0) or 0)
        test_result_id = result.get("test_result_id")
        tw_id = result.get("tw_id")
        user_id = result.get("user_id")
        start_ts = time.time()
        tw_id_str = str(tw_id) if tw_id is not None else ""

        def set_work_state(status, note=""):
            if not tw_id_str:
                return
            with jobs_lock:
                j = active_jobs.get(str(jid))
                if not j:
                    return
                ws = j.get("work_state") or {}
                ws[tw_id_str] = {"status": status, "ts": time.time(), "note": note[:120]}
                j["work_state"] = ws

        if cur_pct >= pct_max:
            job_log(jid, f"[solve] Пропуск: {cur_pct:.1f}% уже выше верхней границы {pct_max}%.")
            set_work_state("above_max", f"{cur_pct:.1f}% >= {pct_max}%")
            return
        # Целевой процент — в указанном диапазоне, но не ниже текущего.
        target_pct = min(pct_max, max(pct_min, cur_pct))

        student_login = get_student_login(user_id)
        if not student_login:
            job_log(jid, "[solve] Не удалось получить логин ученика.")
            set_work_state("no_login")
            return
        student_session = login_as_student(student_login, "1234")
        if not student_session:
            # Fallback: reset password from teacher account and retry login once.
            if user_id and force_set_student_password(user_id, "1234"):
                job_log(jid, "[solve] Первый вход не удался. Сбросили пароль и повторяем вход...")
                student_session = login_as_student(student_login, "1234")
            if not student_session:
                job_log(jid, "[solve] Не удалось войти как ученик.")
                set_work_state("login_failed")
                return

        # Если попытка ещё не запущена — стартуем её.
        if not test_result_id and tw_id:
            start_url = f"{BASE_URL}/TestWorkRun/Start/{tw_id}"
            resp_start = student_session.post(start_url, headers=headers, impersonate="chrome120", allow_redirects=True)
            new_tr, new_tw = extract_ids_from_url(resp_start.url)
            if new_tr:
                test_result_id = new_tr
            if new_tw:
                tw_id = new_tw
            if not test_result_id or not tw_id:
                # Попробуем выцепить из текста на всякий случай.
                m = re.search(r"testResultId=(\\d+)", resp_start.text)
                if m:
                    test_result_id = test_result_id or m.group(1)
                m = re.search(r"twId=(\\d+)", resp_start.text)
                if m:
                    tw_id = tw_id or m.group(1)
            if test_result_id and tw_id:
                job_log(jid, f"[solve] Запустили попытку, testResultId={test_result_id}, twId={tw_id}")
            else:
                job_log(jid, "[solve] Не удалось запустить попытку: нет testResultId/twId.")
                set_work_state("start_failed")
                return

        tasks, scored = collect_overview_tasks(student_session, test_result_id, tw_id)
        if not tasks:
            job_log(jid, "[solve] Нет заданий в Overview.")
            set_work_state("no_tasks")
            return
        else:
            for ex_pos, link in tasks:
                ok = solve_task(student_session, test_result_id, tw_id, ex_pos, work_key)
                job_log(jid, f"[solve] Задание {ex_pos}: {'OK' if ok else 'FAIL'}")
                try:
                    res_mid = evaluate_student_in_work(job, work)
                    cur_pct = float(res_mid.get("current_pct", cur_pct) or cur_pct)
                    if cur_pct >= pct_max:
                        job_log(jid, f"[solve] Достигнут потолок {cur_pct:.1f}% >= {pct_max}%. Останавливаем решение.")
                        break
                except Exception:
                    pass
                time.sleep(1)

        # Обновляем факт. процент после решения из учительской страницы.
        try:
            res_after = evaluate_student_in_work(job, work)
            cur_pct = float(res_after.get("current_pct", cur_pct) or cur_pct)
            test_result_id = res_after.get("test_result_id") or test_result_id
            tw_id = res_after.get("tw_id") or tw_id
            job_log(jid, f"[solve] Процент после решения: {cur_pct:.1f}%")
        except Exception as e:
            job_log(jid, f"[solve] Не удалось обновить процент: {e}")

        if cur_pct < pct_min:
            job_log(
                jid,
                f"[solve] Достигнуто {cur_pct:.1f}%, меньше нижней границы {pct_min}%. Завершение отменено.",
            )
            set_work_state("below_min", f"{cur_pct:.1f}% < {pct_min}%")
            return

        wait_sec = random.randint(delay_min, delay_max)
        job_log(
            jid,
            f"[solve] Ждём {wait_sec:.0f}s после решения. Цель {target_pct:.1f}%.",
        )
        time.sleep(wait_sec)

        if test_result_id and tw_id:
            complete_url = f"{BASE_URL}/TestWorkRun/CompleteTest"
            payload = {"testResultId": test_result_id, "twId": tw_id}
            resp_complete = student_session.post(complete_url, data=payload, headers=headers, impersonate="chrome120")
            if resp_complete.status_code == 200:
                job_log(jid, "[solve] Попытка завершена через CompleteTest.")
                notify_job(jid, f"Режим решать: работа '{work.get('title')}' завершена.")
                set_work_state("completed", f"{cur_pct:.1f}%")
                # Если решали конкретную работу — останавливаем задачу целиком.
                if job.get("work_title_filter"):
                    with jobs_lock:
                        if str(jid) in active_jobs:
                            active_jobs[str(jid)]["status"] = "done"
                            active_jobs[str(jid)]["stop_event"].set()
                            append_history({"event": "completed", "job_id": jid, "student": job.get("student_name", "")})
                            del active_jobs[str(jid)]
                    job_log(jid, "[solve] Задача завершена и удалена после успешного завершения работы.")
            else:
                job_log(jid, f"[solve] Не удалось завершить попытку (status {resp_complete.status_code}).")
                set_work_state("complete_failed", str(resp_complete.status_code))
        else:
            job_log(jid, "[solve] Нет test_result_id/tw_id для завершения.")
            set_work_state("no_ids")
    finally:
        with active_solve_lock:
            if work_key in active_solve_threads:
                del active_solve_threads[work_key]


def fetch_running_works(teacher_ids=None):
    teacher_ids = teacher_ids or [""]
    works = []
    for teacher_id in teacher_ids:
        current_works_url = f"{WORKS_URL}&t={teacher_id}"
        resp_works = session.get(current_works_url, headers=headers, impersonate="chrome120")
        soup = BeautifulSoup(resp_works.text, "html.parser")
        rows = soup.find_all("tr", class_="running")
        for row in rows:
            class_cell = row.find("td", class_="class")
            title_cell = row.find("td", class_="title")
            if not class_cell or not title_cell:
                continue
            works.append(
                {
                    "teacher_id": teacher_id or "",
                    "class_name": re.sub(r"\s+", " ", class_cell.get_text(strip=True)),
                    "title": title_cell.get_text(strip=True),
                    "link": row.get("data-href"),
                }
            )
    return works


def fetch_filter_options():
    url = f"{WORKS_URL}&t=&s=all&c=-1"
    resp = session.get(url, headers=headers, impersonate="chrome120")
    soup = BeautifulSoup(resp.text, "html.parser")

    teachers = []
    classes = []

    teacher_select = soup.find("select", {"id": "FilterTeacher"})
    if teacher_select:
        for opt in teacher_select.find_all("option"):
            val = (opt.get("value") or "").strip()
            name = re.sub(r"\s+", " ", opt.get_text(strip=True))
            teachers.append((val, name))

    class_select = soup.find("select", {"id": "FilterClass"})
    if class_select:
        for opt in class_select.find_all("option"):
            val = (opt.get("value") or "").strip()
            name = re.sub(r"\s+", " ", opt.get_text(strip=True))
            if val == "-1":
                continue
            classes.append((val, name))

    return teachers, classes


def fetch_classes_options(teacher_id=""):
    """Fetch available classes for a given teacher filter (or all if teacher_id is empty)."""
    url = f"{WORKS_URL}&t={teacher_id}&s=all&c=-1"
    resp = session.get(url, headers=headers, impersonate="chrome120")
    soup = BeautifulSoup(resp.text, "html.parser")
    classes = []
    class_select = soup.find("select", {"id": "FilterClass"})
    if class_select:
        for opt in class_select.find_all("option"):
            val = (opt.get("value") or "").strip()
            name = re.sub(r"\\s+", " ", opt.get_text(strip=True))
            if not val or val == "-1":
                continue
            classes.append((val, name))
    return classes


def fetch_classes_for_teachers(teacher_ids):
    """Return union of classes across teacher_ids. If teacher_ids=[''] => all classes."""
    teacher_ids = teacher_ids or [""]
    if teacher_ids == [""]:
        return fetch_classes_options("")
    merged = {}
    for t_id in teacher_ids:
        try:
            for c_id, c_name in fetch_classes_options(t_id):
                merged.setdefault(c_id, c_name)
        except Exception:
            continue
    return [(c_id, merged[c_id]) for c_id in sorted(merged.keys())]


def fetch_works_list(teacher_id="", class_id=""):
    url = f"{WORKS_URL}&t={teacher_id}&s=all&c={class_id or -1}"
    resp = session.get(url, headers=headers, impersonate="chrome120")
    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr", class_=re.compile(r"(running|unchecked|checked|new|draft)", re.I))

    works = []
    for row in rows:
        title_cell = row.find("td", class_="title")
        class_cell = row.find("td", class_="class")
        if not title_cell or not class_cell:
            continue
        status = "unknown"
        classes = row.get("class", [])
        for item in ("running", "unchecked", "checked", "new", "draft"):
            if item in classes:
                status = item
                break
        works.append(
            {
                "title": re.sub(r"\s+", " ", title_cell.get_text(strip=True)),
                "class_name": re.sub(r"\s+", " ", class_cell.get_text(strip=True)),
                "status": status,
                "link": row.get("data-href", ""),
            }
        )
    return works


def parse_student_progress(soup_details, s_row, total_max_points):
    results_row = None
    user_id = s_row.get("data-user-id")
    if user_id:
        rows_for_user = soup_details.find_all("tr", class_="data-row", attrs={"data-user-id": user_id})
        for candidate in rows_for_user:
            if candidate.find("div", class_="data-content-result"):
                results_row = candidate
                break
    if not results_row:
        results_row = s_row

    result_cells = results_row.find_all("div", class_="data-content-result")
    total_current_points = 0.0
    solved_tasks = []
    for cell in result_cells:
        try:
            ex_pos = int(cell.get("data-ex-pos"))
            span_pts = cell.find("span", class_="points")
            if span_pts:
                pts = float(span_pts.text.strip().replace(",", "."))
                total_current_points += pts
                if pts > 0:
                    solved_tasks.append((ex_pos, pts))
        except Exception:
            continue

    test_result_id = results_row.get("data-test-result-id") or s_row.get("data-test-result-id")
    if not test_result_id and result_cells:
        tr_attr = result_cells[0].get("data-tr-id")
        if tr_attr:
            test_result_id = tr_attr

    current_pct = (total_current_points / total_max_points * 100.0) if total_max_points > 0 else 0.0
    return total_current_points, current_pct, solved_tasks, test_result_id


def collect_classes(teacher_ids=None):
    return sorted({w["class_name"] for w in fetch_running_works(teacher_ids)})


def collect_students_for_class(class_name, teacher_ids=None):
    def class_matches(target, actual):
        t = target.strip().lower()
        a = actual.strip().lower()
        if t == a:
            return True
        t_num = re.match(r"^(\d+)", t)
        a_num = re.match(r"^(\d+)", a)
        if t_num and a_num and t_num.group(1) == a_num.group(1):
            return True
        return False

    # Cache by class+teachers to prevent repeated slow scans.
    t_key = ",".join(sorted([str(x) for x in (teacher_ids or [""]) ]))
    cache_key = f"class:{class_name.strip().lower()}|t:{t_key}"
    now_ts = time.time()
    with students_cache_lock:
        ent = students_cache.get(cache_key)
        if ent and now_ts - float(ent.get("ts", 0.0) or 0.0) < 600:
            return ent.get("students") or []

    students = {}
    works = fetch_running_works(teacher_ids)
    deadline = now_ts + 20  # hard budget for interactive UI
    checked = 0
    for work in works:
        if time.time() > deadline or checked >= 8:
            break
        if not class_matches(class_name, work["class_name"]):
            continue
        full_link = BASE_URL + work["link"]
        try:
            resp_details = session.get(full_link, headers=headers, impersonate="chrome120", timeout=YK_HTTP_TIMEOUT_SEC)
        except Exception:
            continue
        soup_details = BeautifulSoup(resp_details.text, "html.parser")
        _, total_max_points = extract_max_points(soup_details, work.get("link", ""))
        for s_row in soup_details.find_all("tr", class_="data-row"):
            name_div = s_row.find("div", class_="data-content")
            if not name_div:
                continue
            student_name = re.sub(r"\s+", " ", name_div.get_text(strip=True))
            _, current_pct, _, _ = parse_student_progress(soup_details, s_row, total_max_points)
            students[student_name] = max(students.get(student_name, -1), current_pct)
        checked += 1

    out = sorted(students.items(), key=lambda x: x[0].lower())
    with students_cache_lock:
        students_cache[cache_key] = {"ts": time.time(), "students": out}
    return out


def _parse_pct_text(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "–"):
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


def collect_students_for_work(work_link):
    """Fast path: extract students list from a single /TestWork/Results page."""
    try:
        _tasks, rows = fetch_testwork_results_page(work_link)
    except Exception:
        rows = []
    out = []
    for r in rows or []:
        nm = str(r.get("name") or "").strip()
        pct = _parse_pct_text(r.get("pct"))
        if not nm or pct is None:
            continue
        out.append((nm, pct))
    return sorted(out, key=lambda x: x[0].lower())


def collect_students_from_works(works, max_works=10, time_budget_sec=20):
    """Union students across multiple works results pages (best-effort, bounded time)."""
    now_ts = time.time()
    deadline = now_ts + float(time_budget_sec or 20)
    students = {}
    checked = 0
    for w in works or []:
        if checked >= int(max_works or 10) or time.time() > deadline:
            break
        link = w.get("link") or ""
        if not link:
            continue
        try:
            rows = collect_students_for_work(link)
        except Exception:
            rows = []
        for nm, pct in rows:
            students[nm] = max(students.get(nm, -1), pct)
        checked += 1
    return sorted(students.items(), key=lambda x: x[0].lower())


def evaluate_student_in_work(job, work):
    full_link = BASE_URL + work["link"]
    resp_details = session.get(full_link, headers=headers, impersonate="chrome120")
    soup_details = BeautifulSoup(resp_details.text, "html.parser")
    _, total_max_points = extract_max_points(soup_details, work.get("link", ""))
    student_rows = soup_details.find_all("tr", class_="data-row")

    for s_row in student_rows:
        name_div = s_row.find("div", class_="data-content")
        if not name_div:
            continue
        student_name = re.sub(r"\s+", " ", name_div.get_text(strip=True))
        if job["student_name"].lower() not in student_name.lower():
            continue

        # Some result tables don't include per-student status (<em>...), so keep it optional.
        status = s_row.find("em").text.strip() if s_row.find("em") else ""
        user_id = s_row.get("data-user-id")
        tw_id = None
        link = work.get("link", "") or ""
        match_tw = re.search(r"/Results/(\d+)", link) or re.search(r"twId=(\d+)", link)
        if match_tw:
            tw_id = match_tw.group(1)

        # NOTE: Do NOT infer "finished" from time/grade cells: those can be present for in-progress attempts too.
        # We'll treat "finished" as unknown here; higher-level code can mark finished based on stronger signals.
        finished = False

        total_current_points, current_pct, solved_tasks, test_result_id = parse_student_progress(
            soup_details, s_row, total_max_points
        )
        if not status:
            status = "Начал(а) работу" if test_result_id else "Не начато"

        # No hysteresis by default: trigger sabotage immediately when above target.
        allowed_pct = min(100, job["target_percentage"])
        allowed_points = (allowed_pct / 100.0) * total_max_points if total_max_points else 0
        projected_points = total_current_points
        tasks_to_spoil = []

        if total_max_points > 0 and projected_points > allowed_points and solved_tasks:
            # Обрабатываем задания в порядке убывания набранных баллов, чтобы гарантированно
            # сбросить процент ниже целевого (без случайностей и пропусков заданий).
            solved_tasks_sorted = sorted(solved_tasks, key=lambda x: x[1], reverse=True)
            for ex_pos, pts in solved_tasks_sorted:
                if projected_points <= allowed_points:
                    break
                tasks_to_spoil.append(ex_pos)
                projected_points -= pts

        # Логируем, какие задания считаем решёнными, чтобы проще отлавливать случаи,
        # когда страница результатов отдаёт не все задачи.
        job_id = job.get("id")
        if job_id and solved_tasks:
            try:
                job_log(job_id, f"Решённые задания: {[(ex, pts) for ex, pts in solved_tasks]}")
            except Exception:
                pass

        return {
            "matched": True,
            "status": status,
            "sabotage": bool(tasks_to_spoil and test_result_id and user_id),
            "tasks_to_spoil": tasks_to_spoil,
            "test_result_id": test_result_id,
            "tw_id": tw_id,
            "user_id": user_id,
            "work_title": work["title"],
            "work_key": f"{work['title']}_{user_id}_{test_result_id}",
            "current_pct": current_pct,
            "finished": finished,
        }

    return {"matched": False}


def monitor_job_loop(job_id):
    while True:
        with jobs_lock:
            job = active_jobs.get(job_id)
            if not job:
                return
            if job["stop_event"].is_set():
                job["status"] = "stopped"
                append_history({"event": "stopped", "job_id": job_id, "student": job["student_name"]})
                job_log(job_id, "Задача остановлена")
                notify_job(job_id, f"Задача #{job_id} остановлена.")
                del active_jobs[job_id]
                return

        try:
            works = fetch_running_works(job["teacher_ids"])
            target_class = job["class_name"].strip().lower()
            target_num = re.match(r"^(\d+)", target_class)
            filtered = []
            for w in works:
                w_class = w["class_name"].strip().lower()
                if w_class == target_class:
                    filtered.append(w)
                    continue
                w_num = re.match(r"^(\d+)", w_class)
                if target_num and w_num and target_num.group(1) == w_num.group(1):
                    filtered.append(w)
            works = filtered
            if job.get("work_title_filter"):
                pattern = job["work_title_filter"].lower()
                works = [w for w in works if pattern in w["title"].lower()]
            if not works:
                job_log(job_id, f"Нет активных работ для класса {job['class_name']}")
                if should_send_periodic_notice(job_id, "last_no_works_notice_ts", 900):
                    notify_job(job_id, f"Нет активных работ для класса {job['class_name']}.")
                time.sleep(30)
                continue

            student_found = False
            for work in works:
                result = evaluate_student_in_work(job, work)
                if not result.get("matched"):
                    continue

                student_found = True
                # Log status/work context for the student's account timeline (no nested jobs_lock).
                cur_status = str(result.get("status") or "")
                cur_work = str(work.get("title") or "")
                pending_logs = []
                with jobs_lock:
                    j = active_jobs.get(job_id)
                    if j:
                        prev_status = str(j.get("last_status") or "")
                        prev_work = str(j.get("last_work_title") or "")
                        if cur_status and cur_status != prev_status:
                            j["last_status"] = cur_status
                            pending_logs.append(f"Статус: {cur_status}")
                        if cur_work and cur_work != prev_work:
                            j["last_work_title"] = cur_work
                            pending_logs.append(f"Работа: {cur_work}")
                for line in pending_logs:
                    job_log(job_id, line)
                if "current_pct" in result:
                    pct_logs = []
                    with jobs_lock:
                        if job_id in active_jobs:
                            j = active_jobs[job_id]
                            prev_pct = float(j.get("last_percentage", 0.0) or 0.0)
                            cur_pct = float(result["current_pct"] or 0.0)
                            j["last_percentage"] = cur_pct
                            j["last_seen"] = now_str()
                            last_logged = j.get("last_pct_logged")
                            last_logged_val = float(last_logged) if last_logged is not None else None
                            if last_logged_val is None or abs(cur_pct - last_logged_val) >= 0.1:
                                j["last_pct_logged"] = cur_pct
                                pct_logs.append(f"Процент: {prev_pct:.1f}% -> {cur_pct:.1f}%")
                    for line in pct_logs:
                        job_log(job_id, line)

                    if result["current_pct"] > job["target_percentage"] and not job.get("alert_sent"):
                        msg = (
                            f"Превышение процента у {job['student_name']}: "
                            f"{result['current_pct']:.1f}% > {job['target_percentage']}%"
                        )
                        job_log(job_id, msg)
                        notify_job(job_id, msg, with_alert_icon=True)
                        with jobs_lock:
                            if job_id in active_jobs:
                                active_jobs[job_id]["alert_sent"] = True

                    if result["current_pct"] <= job["target_percentage"]:
                        if job.get("alert_sent"):
                            notify_job(
                                job_id,
                                f"Процент снова в норме: {result['current_pct']:.1f}% <= {job['target_percentage']}%",
                            )
                        with jobs_lock:
                            if job_id in active_jobs:
                                active_jobs[job_id]["alert_sent"] = False

                if job.get("mode") == "sabotage" and result.get("sabotage"):
                    work_key = result["work_key"]
                    start_thread = False
                    with active_sabotage_lock:
                        if work_key not in active_sabotage_threads:
                            active_sabotage_threads[work_key] = True
                            start_thread = True

                    if start_thread:
                        job_log(job_id, f"Запуск обработки {result['work_title']} | план: {result['tasks_to_spoil']}")
                        if result["current_pct"] > job["target_percentage"]:
                            notify_job(
                                job_id,
                                f"Запуск обработки '{result['work_title']}', задания: {result['tasks_to_spoil']}",
                            )
                        else:
                            notify_job(
                                job_id,
                                f"Найдена работа для порчи: {result['work_title']} (статус {result.get('status','')}). "
                                f"Задания: {result['tasks_to_spoil']}",
                            )
                        threading.Thread(
                            target=worker_sabotage,
                            args=(
                                result["user_id"],
                                job["student_name"],
                                result["work_title"],
                                work_key,
                                job["target_percentage"],
                                result["tasks_to_spoil"],
                                result["test_result_id"],
                                result["tw_id"],
                                job_id,
                            ),
                            daemon=True,
                        ).start()
                elif job.get("mode") == "solve":
                    if not result.get("user_id"):
                        job_log(job_id, "[solve] Пропуск: нет user_id (вероятно, попытка завершена или ученик не начинал).")
                        continue
                    tw_id = str(result.get("tw_id") or "")
                    # Stable per-work key for anti-spam notifications in solve mode.
                    # Prefer twId when present; otherwise fall back to (title + user).
                    solve_track_key = (
                        f"tw:{tw_id}"
                        if tw_id
                        else f"wu:{str(result.get('work_title') or work.get('title') or '').strip().lower()}|{result.get('user_id')}"
                    )
                    if tw_id:
                        with jobs_lock:
                            j = active_jobs.get(str(job_id))
                            ws = (j or {}).get("work_state") or {}
                        st = (ws.get(tw_id) or {}).get("status")
                        if st in ("completed", "no_tasks", "below_min", "above_max", "finished"):
                            # Already handled; don't spam the same work forever in "all works" mode.
                            continue
                        if result.get("finished"):
                            with jobs_lock:
                                j = active_jobs.get(str(job_id))
                                if j is not None:
                                    ws = j.get("work_state") or {}
                                    ws[tw_id] = {"status": "finished", "ts": time.time(), "note": "attempt finished"}
                                    j["work_state"] = ws
                            continue
                    work_key = result.get("work_key") or f"{work.get('title')}_{result.get('user_id')}_{result.get('test_result_id')}"
                    start_thread = False
                    notify_once = True
                    with active_solve_lock:
                        if work_key not in active_solve_threads:
                            active_solve_threads[work_key] = True
                            start_thread = True
                    # Mark and check per-work notify flag under jobs_lock.
                    with jobs_lock:
                        j = active_jobs.get(str(job_id))
                        if j is not None:
                            ws = j.get("work_state") or {}
                            entry = ws.get(solve_track_key) or {}
                            notify_once = not bool(entry.get("notify_sent"))
                            if notify_once:
                                entry["notify_sent"] = True
                                entry["ts"] = time.time()
                                if not entry.get("status"):
                                    entry["status"] = "seen"
                                ws[solve_track_key] = entry
                                j["work_state"] = ws
                    if start_thread:
                        job_log(job_id, f"Запуск решения {result.get('work_title', work.get('title',''))}")
                        if notify_once:
                            notify_job(
                                job_id,
                                f"Найдена работа для решения: {result.get('work_title', work.get('title',''))} "
                                f"(статус {result.get('status','')}, {result.get('current_pct',0):.1f}%).",
                            )
                        threading.Thread(
                            target=worker_solve,
                            args=(job, work, result, work_key, job_id),
                            daemon=True,
                        ).start()

            if not student_found:
                job_log(job_id, f"Ученик {job['student_name']} не найден в активных работах")
                if should_send_periodic_notice(job_id, "last_not_found_notice_ts", 900):
                    notify_job(job_id, f"Ученик {job['student_name']} не найден в активных работах.")

            time.sleep(45)
        except Exception as e:
            job_log(job_id, f"Ошибка цикла мониторинга: {e}")
            if should_send_periodic_notice(job_id, "last_error_notice_ts", 600):
                notify_job(job_id, f"Ошибка цикла мониторинга: {e}", with_alert_icon=True)
            time.sleep(45)


def build_job_key(class_name, student_name):
    # Only one active job per student (global), regardless of class.
    return student_name.strip().lower()


def create_job(
    class_name,
    student_name,
    target_percentage,
    teacher_ids=None,
    work_title_filter="",
    chat_id=None,
    mode="sabotage",
    pct_min=0,
    pct_max=0,
    delay_min_sec=60,
    delay_max_sec=180,
    monitor_new_tasks=False,
):
    global job_counter

    if not teacher_ids:
        teacher_ids = [""]

    with jobs_lock:
        key = build_job_key(class_name, student_name)
        for j in active_jobs.values():
            if j.get("key") == key:
                return (
                    None,
                    f"Конфликт: уже есть активная задача #{j.get('id')} "
                    f"(режим {j.get('mode')}, работа '{j.get('work_title_filter') or 'все'})'. "
                    "Нельзя создавать портить/решать одновременно для одного ученика.",
                )

        job_counter += 1
        job_id = str(job_counter)
        stop_event = threading.Event()

        job = {
            "id": job_id,
            "key": key,
            "class_name": class_name,
            "student_name": student_name,
            "target_percentage": max(0, min(100, int(target_percentage))),
            "teacher_ids": teacher_ids,
            "work_title_filter": work_title_filter.strip(),
            "created_at": now_str(),
            "last_seen": "-",
            "last_percentage": 0.0,
            "last_pct_logged": None,
            "last_status": "",
            "last_work_title": "",
            "status": "running",
            "alert_sent": False,
            "stop_event": stop_event,
            "chat_id": str(chat_id) if chat_id else None,
            "last_no_works_notice_ts": 0.0,
            "last_not_found_notice_ts": 0.0,
            "last_error_notice_ts": 0.0,
            "last_sabotage_notify_ts": 0.0,
            "mode": "solve" if str(mode).lower().startswith("solve") else "sabotage",
            "pct_min": int(pct_min) if pct_min is not None else 0,
            "pct_max": int(pct_max) if pct_max is not None else 0,
            "delay_min_sec": int(delay_min_sec) if delay_min_sec is not None else 60,
            "delay_max_sec": int(delay_max_sec) if delay_max_sec is not None else 180,
            "monitor_new_tasks": bool(monitor_new_tasks),
            # Per-work memory for "all works" monitoring to avoid re-solving the same work forever.
            # Key: tw_id (string). Value: {"status": str, "ts": float, "note": str}
            "work_state": {},
        }

        thread = threading.Thread(target=monitor_job_loop, args=(job_id,), daemon=True)
        job["thread"] = thread
        active_jobs[job_id] = job
        thread.start()

    append_history(
        {
            "event": "created",
            "job_id": job_id,
            "class_name": class_name,
            "student_name": student_name,
            "target_percentage": target_percentage,
            "mode": job["mode"],
            "chat_id": str(chat_id) if chat_id else None,
        }
    )
    w_filter = f", работа '{work_title_filter}'" if work_title_filter else ", все работы"
    job_log(
        job_id,
        f"Создана задача для {student_name}, класс {class_name}, режим {job['mode']}, цель {target_percentage}%{w_filter}",
    )
    notify_job(
        job_id,
        f"Задача #{job_id} создана: {student_name}, класс {class_name}, режим {job['mode']}, цель {target_percentage}%{w_filter}",
    )
    return job_id, None


def stop_job(job_id):
    job_id = str(job_id)
    with jobs_lock:
        job = active_jobs.get(job_id)
        if not job:
            return False
        if job.get("status") in ("stopping", "stopped"):
            return True
        job["stop_event"].set()
        job["status"] = "stopped"
        chat_id = job.get("chat_id")
        student = job.get("student_name", "")
        cls = job.get("class_name", "")
        target = job.get("target_percentage", "")
        # Remove immediately so "Активные задачи" stays clean.
        del active_jobs[job_id]

    append_history(
        {
            "event": "stopped",
            "job_id": job_id,
            "student_name": student,
            "class_name": cls,
            "target_percentage": target,
            "chat_id": str(chat_id) if chat_id else None,
        }
    )
    job_log(job_id, "Задача остановлена (по запросу)")
    # notify_job reads active_jobs, so send directly.
    if chat_id:
        send_tg_message(chat_id, f"Задача #{job_id} остановлена.")
    return True


def active_jobs_text():
    with jobs_lock:
        if not active_jobs:
            return "Активных задач нет."
        lines = ["Активные задачи:"]
        for job_id, job in sorted(active_jobs.items(), key=lambda x: int(x[0])):
            work_label = job.get("work_title_filter") or "все работы"
            lines.append(
                f"#{job_id} | {job['class_name']} | {job['student_name']} | "
                f"{work_label} | режим {job.get('mode','sabotage')} | "
                f"цель {job['target_percentage']}% | "
                f"текущее {job['last_percentage']:.1f}% | {job['status']}"
            )
        lines.append("Остановить: stop &lt;id&gt;")
        return "\n".join(lines)


def active_jobs_keyboard(mode="view"):
    """Build inline keyboard for actions on active jobs; in logs mode also add archived logs."""
    with jobs_lock:
        jobs = sorted(active_jobs.items(), key=lambda x: int(x[0]))

    # Collect archived log ids (job_*.log) for logs mode.
    archived = []
    if mode == "logs":
        try:
            for fname in os.listdir(LOGS_DIR):
                if not fname.startswith("job_") or not fname.endswith(".log"):
                    continue
                jid = fname[4:-4]
                if jid and not any(jid == j[0] for j in jobs):
                    archived.append(jid)
            archived = sorted(archived, key=lambda x: int(x))
        except Exception:
            archived = []

    if not jobs and not archived:
        return None

    kb = []
    for job_id, job in jobs:
        label = f"#{job_id} {job['student_name']} {job['last_percentage']:.1f}%"
        row = [{"text": label, "callback_data": f"job:menu:{job_id}"}]
        if mode == "logs":
            row = [{"text": f"📎 Лог #{job_id}", "callback_data": f"job:log:{job_id}"}]
        elif mode == "stop":
            row = [{"text": f"⛔ Стоп #{job_id}", "callback_data": f"job:stop:{job_id}"}]
        elif mode == "delhist":
            row = [{"text": f"🗑️ История #{job_id}", "callback_data": f"job:delhist:{job_id}"}]
        kb.append(row)

    if mode == "logs":
        for jid in archived:
            kb.append([{"text": f"📎 Лог #{jid} (архив)", "callback_data": f"job:log:{jid}"}])

    return kb


def history_text(limit=20):
    ensure_runtime_files()
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return "История недоступна."

    if not data:
        return "История пуста."

    tail = data[-limit:]
    lines = ["Последние события:"]
    for event in tail:
        lines.append(
            f"{event.get('time', '?')} | {event.get('event', '?')} | "
            f"job={event.get('job_id', '?')} | {event.get('student_name', event.get('student', ''))}"
        )
    return "\n".join(lines)


def logs_text(job_id, limit=30):
    path = os.path.join(LOGS_DIR, f"job_{job_id}.log")
    if not os.path.exists(path):
        return f"Логов для задачи {job_id} нет."

    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    tail = lines[-limit:]
    return "".join(tail) if tail else "Лог пуст."


def tg_request(method, payload=None, is_get=False):
    if not TELEGRAM_BOT_TOKEN:
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        # Use stdlib networking for Telegram to avoid curl_cffi thread-safety issues with Yaklass monitoring.
        timeout = 80 if (is_get and method == "getUpdates") else 25
        if is_get:
            qs = urllib.parse.urlencode(payload or {})
            req = urllib.request.Request(url + ("?" + qs if qs else ""), method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        else:
            body = json.dumps(payload or {}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict) and not data.get("ok"):
            print(f"[TG] API error {method}: {data}")
        return data
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            print(f"[TG] HTTP error {method}: {e.code} {e.reason} body={raw}")
        except Exception:
            print(f"[TG] HTTP error {method}: {e}")
        return None
    except Exception as e:
        print(f"[TG] Ошибка запроса {method}: {e}")
        return None


def tg_send_document(chat_id, file_bytes, filename="file.txt", caption=None):
    """Send a file to Telegram via sendDocument using multipart/form-data."""
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    boundary = f"----tg{random.randint(10**9, 10**10-1)}"

    def _b(s):
        return s.encode("utf-8")

    # Build multipart body.
    parts = []

    def add_field(name, value):
        parts.append(_b(f"--{boundary}\r\n"))
        parts.append(_b(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'))
        parts.append(_b(str(value)))
        parts.append(_b("\r\n"))

    def add_file(field, fn, data, mime="text/plain"):
        parts.append(_b(f"--{boundary}\r\n"))
        parts.append(
            _b(
                f'Content-Disposition: form-data; name="{field}"; filename="{fn}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            )
        )
        parts.append(data)
        parts.append(_b("\r\n"))

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
    add_file("document", filename, file_bytes, mime="text/plain")
    parts.append(_b(f"--{boundary}--\r\n"))

    body = b"".join(parts)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            print(f"[TG] HTTP error sendDocument: {e.code} {e.reason} body={raw}")
        except Exception:
            print(f"[TG] HTTP error sendDocument: {e}")
        return None
    except Exception as e:
        print(f"[TG] Ошибка запроса sendDocument: {e}")
        return None


def send_job_log_file(chat_id, job_id):
    """Send full log file (or a tail if huge) as a Telegram document."""
    ensure_runtime_files()
    jid = str(job_id).strip()
    path = os.path.join(LOGS_DIR, f"job_{jid}.log")
    if not os.path.exists(path):
        send_tg_message(chat_id, f"Лог для задачи #{jid} не найден.", parse_mode=None)
        return

    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0

    max_send = 8 * 1024 * 1024  # keep safe margin vs Telegram limits and memory
    filename = f"job_{jid}.log"
    caption = f"Лог задачи #{jid}"
    try:
        with open(path, "rb") as fh:
            if size > max_send:
                # Send tail to keep it bounded.
                fh.seek(max(0, size - max_send))
                data = fh.read(max_send)
                filename = f"job_{jid}_tail.log"
                caption += f" (последние ~{max_send//1024//1024}MB, файл слишком большой)"
            else:
                data = fh.read()
    except Exception as e:
        send_tg_message(chat_id, f"Не удалось прочитать лог #{jid}: {e}", parse_mode=None)
        return

    resp = tg_send_document(chat_id, data, filename=filename, caption=caption)
    if not resp or not resp.get("ok"):
        # Fallback to text tail.
        send_tg_message(chat_id, logs_text(jid), parse_mode=None)


def main_menu_keyboard(admin=False):
    rows = [
        ["➕ Новая задача", "📊 Активные задачи"],
        ["📋 Результаты"],
        ["📈 Проценты", "🕘 История"],
        ["🧾 Логи", "⛔ Остановить"],
        ["🗑️ Удалить из истории"],
        ["ℹ️ Меню"],
    ]
    if admin:
        rows.append(["🛂 Заявки", "🧹 Очистить историю"])
    return rows


def flow_keyboard():
    return [["❌ Отмена"], ["ℹ️ Меню"]]


def _inline_markup(inline_keyboard):
    if not inline_keyboard:
        return None
    return {"inline_keyboard": inline_keyboard}


def send_tg_message(chat_id, text, keyboard=None, inline_keyboard=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if keyboard:
        payload["reply_markup"] = {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    if inline_keyboard:
        payload["reply_markup"] = _inline_markup(inline_keyboard)
    return tg_request("sendMessage", payload=payload)


def edit_tg_message(chat_id, message_id, text, inline_keyboard=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if inline_keyboard:
        payload["reply_markup"] = _inline_markup(inline_keyboard)
    return tg_request("editMessageText", payload=payload)


def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:190]
    if show_alert:
        payload["show_alert"] = True
    return tg_request("answerCallbackQuery", payload=payload)


def send_active_jobs_view(chat_id, mode="view"):
    kb = active_jobs_keyboard(mode="view" if mode == "view" else mode)
    if not kb:
        send_tg_message(chat_id, "Активных задач нет.")
        return

    header = {
        "view": "<b>Активные задачи</b>",
        "logs": "<b>Выберите задачу для лога</b>",
        "stop": "<b>Выберите задачу для остановки</b>",
        "delhist": "<b>Удалить историю задачи</b>",
    }.get(mode, "<b>Активные задачи</b>")

    send_tg_message(chat_id, header, inline_keyboard=kb)


def send_job_actions(chat_id, job_id):
    with jobs_lock:
        job = active_jobs.get(str(job_id))
    if not job:
        send_tg_message(chat_id, f"Задача #{job_id} не найдена или уже завершена.")
        return
    text = (
        f"<b>Задача #{job_id}</b>\n"
        f"{html.escape(job.get('student_name', ''))}, {html.escape(job.get('class_name', ''))}\n"
        f"Работа: {html.escape(job.get('work_title_filter') or 'все работы')}\n"
        f"Цель: {job.get('target_percentage', 0)}% | Текущее: {job.get('last_percentage', 0.0):.1f}%"
    )
    kb = [
        [
            {"text": "📎 Лог файлом", "callback_data": f"job:log:{job_id}"},
            {"text": "📝 Лог текстом", "callback_data": f"job:logtxt:{job_id}"},
            {"text": "⛔ Стоп", "callback_data": f"job:stop:{job_id}"},
        ]
    ]
    send_tg_message(chat_id, text, inline_keyboard=kb)


def send_main_menu(chat_id):
    admin = is_admin(chat_id)
    text = (
        "<b>Панель управления</b>\n"
        "\n"
        "➕ Новая задача\n"
        "📊 Активные задачи\n"
        "📈 Проценты\n"
        "🕘 История\n"
        "🧾 Логи (кнопки)\n"
        "⛔ Остановить (кнопки)\n"
        "🗑️ Удалить из истории (кнопки)"
    )
    if admin:
        text += "\n🛂 Заявки\n🧹 Очистить историю"
    send_tg_message(chat_id, text, keyboard=main_menu_keyboard(admin=admin))


def normalize_user_input(text):
    raw = (text or "").strip()
    lower = raw.lower()
    mapping = {
        "➕ новая задача": "new",
        "📊 активные задачи": "active",
        "📋 результаты": "results",
        "📈 проценты": "percent",
        "🕘 история": "history",
        "🧾 логи": "logs_help",
        "⛔ остановить": "stop_help",
        "ℹ️ меню": "menu",
        "🛂 заявки": "requests",
        "🗑️ удалить из истории": "delhist_help",
        "🧹 очистить историю": "clearhist",
        "❌ отмена": "cancel",
    }
    return raw, mapping.get(lower, lower)


def _chunk_buttons(items, per_row=2):
    rows = []
    row = []
    for btn in items:
        row.append(btn)
        if len(row) >= per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _pager_row(prefix, page, total_pages):
    nav = []
    if total_pages > 1:
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": f"{prefix}:p:{page-1}"})
        nav.append({"text": f"{page+1}/{total_pages}", "callback_data": f"{prefix}:noop"})
        if page + 1 < total_pages:
            nav.append({"text": "➡️", "callback_data": f"{prefix}:p:{page+1}"})
    return [nav] if nav else []


def _trim_button_text(s, max_len=32):
    s = re.sub(r"\\s+", " ", (s or "")).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def build_teacher_keyboard(state):
    teachers = state.get("teachers") or []
    selected = state.get("teacher_selected") or set()
    page = int(state.get("teacher_page", 0) or 0)

    per_page = 10
    total_pages = max(1, (len(teachers) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    state["teacher_page"] = page

    start = page * per_page
    end = min(len(teachers), start + per_page)
    buttons = []
    for idx in range(start, end):
        _, t_name = teachers[idx]
        mark = "✅ " if idx in selected else ""
        buttons.append({"text": _trim_button_text(f"{mark}{t_name}", 36), "callback_data": f"teach:t:{idx}"})

    rows = _chunk_buttons(buttons, per_row=1)
    rows += _pager_row("teach", page, total_pages)

    all_mark = "✅ " if state.get("teacher_all") else ""
    rows.append(
        [
            {"text": f"{all_mark}Все учителя", "callback_data": "teach:all"},
            {"text": "Далее ➡️", "callback_data": "teach:next"},
        ]
    )
    rows.append([{"text": "❌ Отмена", "callback_data": "flow:cancel"}])
    return rows


def build_class_keyboard(state):
    classes = state.get("classes") or []
    page = int(state.get("class_page", 0) or 0)
    per_page = 10
    total_pages = max(1, (len(classes) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    state["class_page"] = page

    start = page * per_page
    end = min(len(classes), start + per_page)
    buttons = []
    for idx in range(start, end):
        _, c_name = classes[idx]
        buttons.append({"text": _trim_button_text(c_name, 36), "callback_data": f"class:t:{idx}"})

    rows = _chunk_buttons(buttons, per_row=1)
    rows += _pager_row("class", page, total_pages)
    rows.append([{"text": "⬅️ Назад", "callback_data": "class:back"}, {"text": "❌ Отмена", "callback_data": "flow:cancel"}])
    return rows


def build_work_keyboard(state):
    works = state.get("works") or []
    page = int(state.get("work_page", 0) or 0)
    per_page = 8
    total_pages = max(1, (len(works) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    state["work_page"] = page

    start = page * per_page
    end = min(len(works), start + per_page)
    buttons = []

    teacher_map = state.get("teacher_map") or {}
    show_teacher = bool(state.get("teacher_ids")) and state.get("teacher_ids") not in ([""], None) and len(state.get("teacher_ids")) > 1
    for idx in range(start, end):
        w = works[idx]
        status = (w.get("status") or "unknown").lower()
        # Avoid "✅" here because users often interpret it as "selected".
        status_ico = {"running": "🟢", "unchecked": "🟡", "checked": "🔵", "new": "🆕", "draft": "📝"}.get(status, "🔹")
        title = w.get("title") or ""
        prefix = ""
        if show_teacher:
            t_label = teacher_map.get(w.get("teacher_id"), w.get("teacher_id", "")).strip()
            if t_label:
                prefix = f"{_trim_button_text(t_label, 16)} · "
        btn_text = _trim_button_text(f"{status_ico} {prefix}{title}", 40)
        buttons.append({"text": btn_text, "callback_data": f"work:t:{idx}"})

    rows = _chunk_buttons(buttons, per_row=1)
    rows += _pager_row("work", page, total_pages)
    rows.append([{"text": "0 · Все работы", "callback_data": "work:all"}])
    rows.append([{"text": "⬅️ Назад", "callback_data": "work:back"}, {"text": "❌ Отмена", "callback_data": "flow:cancel"}])
    return rows


def build_student_keyboard(state):
    students = state.get("students") or []
    page = int(state.get("student_page", 0) or 0)
    per_page = 10
    total_pages = max(1, (len(students) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    state["student_page"] = page

    start = page * per_page
    end = min(len(students), start + per_page)
    buttons = []
    for idx in range(start, end):
        name, pct = students[idx]
        btn_text = _trim_button_text(f"{name} ({pct:.1f}%)", 40)
        buttons.append({"text": btn_text, "callback_data": f"stud:t:{idx}"})

    rows = _chunk_buttons(buttons, per_row=1)
    rows += _pager_row("stud", page, total_pages)
    rows.append([{"text": "⬅️ Назад", "callback_data": "stud:back"}, {"text": "❌ Отмена", "callback_data": "flow:cancel"}])
    return rows


def fetch_testwork_results_page(work_link):
    """Fetch /TestWork/Results page: returns tasks metadata and per-student breakdown."""
    if not work_link:
        return [], []
    full_link = work_link if work_link.startswith("http") else BASE_URL + work_link
    resp = session.get(full_link, headers=headers, impersonate="chrome120", timeout=YK_HTTP_TIMEOUT_SEC)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Tasks header (numbers, titles, max points, avg %).
    tasks = []
    header_ths = soup.select("table#resultsHeader table.results-table thead tr.header-row th")
    info_cells = soup.select("table#resultsHeader table.results-table thead tr.info-row td")
    for idx, th in enumerate(header_ths):
        raw_title = th.get("title", "") or th.get_text(strip=True)
        title_attr = re.sub(r"<.*?>", "", raw_title)
        max_pts = ""
        m = re.search(r"\(([\d.,]+)\s*<span", title_attr) or re.search(r"\(([\d.,]+)\s*Б", title_attr)
        if m:
            max_pts = m.group(1).replace(",", ".")
        avg_pct = ""
        if idx < len(info_cells):
            score_span = info_cells[idx].find("span", class_="score")
            if score_span:
                avg_pct = score_span.get_text(strip=True)
        tasks.append(
            {
                "pos": idx + 1,
                "title": re.sub(r"\s+", " ", title_attr).strip()[:90],
                "max": max_pts or "-",
                "avg": avg_pct or "-",
            }
        )

    students = []
    info_rows = soup.select("table#resultsTable td.info-table-container table.info-table tr.data-row")
    task_rows = soup.select("table#resultsTable table.results-table tr.data-row")

    for tr_info, tr_tasks in zip(info_rows, task_rows):
        name = ""
        status = ""
        pct = ""
        points = ""

        name_div = tr_info.find("div", class_="data-content")
        if name_div:
            name = re.sub(r"\\s+", " ", name_div.get_text(strip=True))

        em = tr_info.find("em")
        if em:
            status = re.sub(r"\\s+", " ", em.get_text(strip=True))

        pct_div = tr_info.select_one("div.student-progress div.earned div")
        if pct_div:
            pct = pct_div.get_text(strip=True)
            if not status:
                status = "Есть результат"

        pts_div = tr_info.select_one("div.student-earned")
        if pts_div:
            points = pts_div.get_text(strip=True)

        per_task = []
        task_cells = tr_tasks.select("td.no-padding")
        for cell in task_cells:
            span = cell.find("span", class_="points")
            val = span.get_text(strip=True) if span else ""
            per_task.append(val or "–")

        if not name:
            continue

        students.append(
            {
                "name": name,
                "status": status or "-",
                "pct": pct or "-",
                "points": points or "-",
                "per_task": per_task,
            }
        )

    return tasks, students


def _parse_float_any(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except Exception:
        return None


def _fmt_pts(pt, mx):
    if pt in ("–", "-", "", None):
        return "–"
    pt_f = _parse_float_any(pt)
    mx_f = _parse_float_any(mx)
    if pt_f is None or mx_f is None:
        return str(pt)
    # keep compact (avoid trailing .0)
    def _n(x):
        s = f"{x:.1f}"
        return s[:-2] if s.endswith(".0") else s

    return f"{_n(pt_f)}/{_n(mx_f)}"


def format_results_messages(tasks, results_rows, class_label, work_label, max_students=25, max_tasks=24):
    # Tasks table
    t = tasks[:max_tasks]
    header = f"<b>Задания</b> · {class_label} · {work_label}"
    lines = [header, ""]
    lines.append("<pre>№  max  ср.%  задание</pre>")
    lines.append("<pre>-- ---- ----  ------------------------------</pre>")
    for item in t:
        pos = int(item.get("pos") or 0)
        mx = str(item.get("max") or "-")
        avg = str(item.get("avg") or "-").replace(" ", "")
        title = str(item.get("title") or "")
        title = re.sub(r"\s+", " ", title).strip()
        lines.append(
            "<pre>"
            f"{pos:02d} {mx:>4} {avg:>4}  {html.escape(title)[:30]}"
            "</pre>"
        )
    if len(tasks) > max_tasks:
        lines.append(f"... и еще {len(tasks) - max_tasks}")
    task_msg = "\n".join(lines)

    # Students breakdown
    mx_by_pos = {int(it.get("pos") or 0): it.get("max") for it in tasks}
    s_header = f"<b>Ученики</b> · {class_label} · {work_label}"
    s_lines = [s_header, ""]
    s_lines.append("<i>Формат: №: набрано/макс по каждому заданию</i>")
    for i, r in enumerate(results_rows[:max_students], start=1):
        name = html.escape(str(r.get("name") or ""))
        pct = html.escape(str(r.get("pct") or "-"))
        points = html.escape(str(r.get("points") or "-"))
        status = html.escape(str(r.get("status") or "-"))
        s_lines.append(f"{i}. <b>{name}</b> — <b>{pct}</b> — {points} б. ({status})")

        per = r.get("per_task") or []
        chunk = []
        for pos, pt in enumerate(per[:max_tasks], start=1):
            mx = mx_by_pos.get(pos, "-")
            chunk.append(f"{pos:02d}:{_fmt_pts(pt, mx)}")
        # wrap to a few lines
        for k in range(0, len(chunk), 8):
            s_lines.append("<pre>" + "  ".join(chunk[k : k + 8]) + "</pre>")
        s_lines.append("")

    if len(results_rows) > max_students:
        s_lines.append(f"... и еще {len(results_rows) - max_students}")
    students_msg = "\n".join([ln for ln in s_lines if ln is not None])
    return task_msg[:3900], students_msg[:3900]


def _flow_msg_id(state):
    try:
        return int(state.get("flow_msg_id") or 0) or None
    except Exception:
        return None


def _teachers_summary(state):
    if state.get("teacher_all"):
        return "все учителя"
    teachers = state.get("teachers") or []
    selected = sorted(state.get("teacher_selected") or set())
    names = []
    for idx in selected[:4]:
        try:
            _, name = teachers[idx]
            names.append(name)
        except Exception:
            continue
    if not selected:
        return "не выбрано"
    if len(selected) <= 4:
        return ", ".join(names)
    return ", ".join(names) + f" +{len(selected)-4}"


def render_flow(chat_id, state, force_send=False):
    step = state.get("step")

    if step == "pick_mode_btn":
        text = (
            "<b>Новая задача</b>\n\n"
            "Выберите режим:\n"
            "• Портить — понижать процент\n"
            "• Решать — выполнять задания до заданного диапазона процента и времени"
        )
        kb = [
            [{"text": "💣 Портить", "callback_data": "mode:sabotage"}],
            [{"text": "🧠 Решать", "callback_data": "mode:solve"}],
            [{"text": "❌ Отмена", "callback_data": "flow:cancel"}],
        ]
    elif step == "pick_teacher_btn":
        text = (
            "<b>Создание задачи</b>\n\n"
            "<i>Шаг 1/5</i>: выберите учителей (можно несколько)\n"
            f"Выбрано: <b>{html.escape(_teachers_summary(state))}</b>"
        )
        kb = build_teacher_keyboard(state)
    elif step == "pick_class_btn":
        text = (
            "<b>Создание задачи</b>\n\n"
            "<i>Шаг 2/5</i>: выберите класс\n"
            f"Учителя: <b>{html.escape(state.get('teacher_name') or _teachers_summary(state))}</b>"
        )
        kb = build_class_keyboard(state)
    elif step == "pick_work_btn":
        cls = state.get("class_name") or ""
        text = (
            "<b>Создание задачи</b>\n\n"
            "<i>Шаг 3/5</i>: выберите работу\n"
            f"Класс: <b>{html.escape(cls)}</b>\n"
            "\n"
            "<i>Подсказка:</i> значки слева показывают статус работы (не выбор). Нажмите на работу, чтобы перейти дальше."
        )
        kb = build_work_keyboard(state)
    elif step == "pick_student_btn":
        cls = state.get("class_name") or ""
        w = state.get("work_title_filter") or "все работы"
        text = (
            "<b>Создание задачи</b>\n\n"
            "<i>Шаг 4/5</i>: выберите ученика\n"
            f"Класс: <b>{html.escape(cls)}</b>\n"
            f"Работа: <b>{html.escape(w)}</b>"
        )
        kb = build_student_keyboard(state)
    elif step == "pick_target":
        cls = state.get("class_name") or ""
        w = state.get("work_title_filter") or "все работы"
        student = state.get("student_name") or ""
        text = (
            "<b>Создание задачи</b>\n\n"
            "<i>Шаг 5/5</i>: введите целевой процент (0-100)\n"
            "\n"
            f"Класс: <b>{html.escape(cls)}</b>\n"
            f"Работа: <b>{html.escape(w)}</b>\n"
            f"Ученик: <b>{html.escape(student)}</b>"
        )
        kb = [[{"text": "❌ Отмена", "callback_data": "flow:cancel"}]]
    else:
        return

    msg_id = _flow_msg_id(state)
    if msg_id and not force_send:
        # If edit fails (message deleted/too old/HTML error), fall back to sending a new message
        # so user doesn't get stuck with dead inline buttons.
        resp = edit_tg_message(chat_id, msg_id, text, inline_keyboard=kb)
        if resp and resp.get("ok"):
            return

    resp = send_tg_message(chat_id, text, inline_keyboard=kb)
    try:
        state["flow_msg_id"] = int(resp.get("result", {}).get("message_id"))
    except Exception:
        state["flow_msg_id"] = None


def handle_callback_query(chat_id, message_id, data, callback_query_id=None):
    # Debug: log every callback so "button does nothing" can be diagnosed.
    try:
        print(f"[TG] Callback chat={chat_id} msg={message_id} data={data}")
    except Exception:
        pass

    answered = False

    def _ack(text=None, alert=False):
        nonlocal answered
        if not callback_query_id or answered:
            return
        answer_callback(callback_query_id, text=text, show_alert=alert)
        answered = True

    # Job actions (logs/stop/menu) доступны вне активного flow.
    if data.startswith("job:"):
        _ack()
        parts = data.split(":")
        if len(parts) >= 3:
            action, job_id = parts[1], parts[2]
            if action == "menu":
                send_job_actions(chat_id, job_id)
            elif action == "log":
                send_job_log_file(chat_id, job_id)
            elif action == "logtxt":
                send_tg_message(chat_id, logs_text(job_id), parse_mode=None)
            elif action == "stop":
                if stop_job(job_id):
                    send_tg_message(chat_id, f"Остановка задачи #{job_id} запрошена.")
                else:
                    send_tg_message(chat_id, f"Задача #{job_id} не найдена.")
            elif action == "delhist":
                ok, msg = delete_history_for_job(job_id, chat_id)
                send_tg_message(chat_id, msg)
        return

    state = telegram_states.get(chat_id)
    if not state:
        _ack("Сессия истекла. Нажмите ℹ️ Меню и начните заново.", alert=False)
        return
    # Keep flow bound to the message user is interacting with.
    # Otherwise, callbacks from an older message may edit a newer message and appear as "nothing happens".
    if message_id:
        try:
            state["flow_msg_id"] = int(message_id)
        except Exception:
            pass

    if data.startswith("mode:"):
        _ack()
        mode = "sabotage" if data.endswith("sabotage") else "solve"
        state["task_mode"] = mode
        if mode == "solve":
            state["step"] = "pct_range"
            send_tg_message(
                chat_id,
                "Режим: 🧠 Решать\nШаг: диапазон процента.\nВведите два числа (мин-макс), пример: 30-55",
            )
        else:
            state["step"] = "pick_teacher_btn"
            render_flow(chat_id, state, force_send=True)
        return

    if data in ("flow:cancel", "cancel"):
        _ack()
        telegram_states.pop(chat_id, None)
        send_main_menu(chat_id)
        return

    # Teachers
    if data.startswith("teach:"):
        _ack()
        if state.get("step") != "pick_teacher_btn":
            return
        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "t":
            try:
                idx = int(parts[2])
            except Exception:
                return
            sel = state.get("teacher_selected") or set()
            if idx in sel:
                sel.remove(idx)
            else:
                sel.add(idx)
            state["teacher_selected"] = sel
            state["teacher_all"] = False
            render_flow(chat_id, state)
            return
        if parts[1] == "all":
            state["teacher_all"] = not bool(state.get("teacher_all"))
            state["teacher_selected"] = set()
            render_flow(chat_id, state)
            return
        if len(parts) >= 3 and parts[1] == "p":
            try:
                state["teacher_page"] = int(parts[2])
            except Exception:
                state["teacher_page"] = 0
            render_flow(chat_id, state)
            return
        if parts[1] == "next":
            teachers = state.get("teachers") or []
            teacher_map = {}
            if state.get("teacher_all"):
                teacher_ids = [""]
                teacher_name = "все учителя"
            else:
                selected = sorted(state.get("teacher_selected") or set())
                if not selected:
                    if callback_query_id:
                        answer_callback(callback_query_id, text="Выберите хотя бы одного учителя.", show_alert=True)
                    return
                teacher_ids = []
                teacher_names = []
                for idx in selected:
                    try:
                        t_id, t_name = teachers[idx]
                    except Exception:
                        continue
                    teacher_ids.append(t_id)
                    teacher_names.append(t_name)
                    teacher_map[t_id] = t_name
                teacher_name = ", ".join(teacher_names)

            state["teacher_ids"] = teacher_ids
            state["teacher_map"] = teacher_map
            state["teacher_name"] = teacher_name
            # Refresh classes based on selected teachers.
            try:
                state["classes"] = fetch_classes_for_teachers(teacher_ids)
            except Exception as e:
                print(f"[Flow] Не удалось обновить классы: {e}")
                state["classes"] = state.get("classes") or []
            state["step"] = "pick_class_btn"
            state["class_page"] = 0
            render_flow(chat_id, state)
            return
        return

    # Classes
    if data.startswith("class:"):
        _ack()
        if data == "class:back":
            state["step"] = "pick_teacher_btn"
            render_flow(chat_id, state)
            return
        if state.get("step") != "pick_class_btn":
            return
        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "t":
            try:
                idx = int(parts[2])
                class_id, class_label = (state.get("classes") or [])[idx]
            except Exception:
                return

            teacher_ids = state.get("teacher_ids") or [""]
            works = []
            if teacher_ids == [""]:
                works = fetch_works_list("", class_id)
            else:
                for t_id in teacher_ids:
                    try:
                        w = fetch_works_list(t_id, class_id)
                        for item in w:
                            item["teacher_id"] = t_id
                        works.extend(w)
                    except Exception:
                        continue

                seen = set()
                unique = []
                for w in works:
                    key = w.get("link") or (w.get("title"), w.get("class_name"), w.get("teacher_id"))
                    if key in seen:
                        continue
                    seen.add(key)
                    unique.append(w)
                works = unique

            state["class_id"] = class_id
            state["class_name"] = works[0]["class_name"] if works else class_label
            state["works"] = works
            state["step"] = "pick_work_btn"
            state["work_page"] = 0
            render_flow(chat_id, state)
            return
        if len(parts) >= 3 and parts[1] == "p":
            try:
                state["class_page"] = int(parts[2])
            except Exception:
                state["class_page"] = 0
            render_flow(chat_id, state)
            return
        return

    # Job actions (logs/stop/menu)
    if data.startswith("job:"):
        _ack()
        parts = data.split(":")
        if len(parts) < 3:
            return
        action, job_id = parts[1], parts[2]
        if action == "menu":
            send_job_actions(chat_id, job_id)
            return
        if action == "log":
            send_tg_message(chat_id, logs_text(job_id), parse_mode=None)
            return
        if action == "stop":
            if stop_job(job_id):
                send_tg_message(chat_id, f"Остановка задачи #{job_id} запрошена.")
            else:
                send_tg_message(chat_id, f"Задача #{job_id} не найдена.")
            return
        if action == "delhist":
            ok, msg = delete_history_for_job(job_id, chat_id)
            send_tg_message(chat_id, msg)
            return
        return

    # Works
    if data.startswith("work:"):
        _ack()
        if data == "work:back":
            state["step"] = "pick_class_btn"
            render_flow(chat_id, state)
            return
        # Allow processing even if state step drifted (e.g. user clicked an old inline keyboard).
        # As long as we still have the works list in state, we can proceed.
        if state.get("step") not in ("pick_work_btn", "pick_student_btn") and not state.get("works"):
            return
        parts = data.split(":")
        if data == "work:all":
            state["work_title_filter"] = ""
        elif len(parts) >= 3 and parts[1] == "t":
            try:
                idx = int(parts[2])
                state["work_title_filter"] = (state.get("works") or [])[idx].get("title", "")
            except Exception:
                return
        elif len(parts) >= 3 and parts[1] == "p":
            try:
                state["work_page"] = int(parts[2])
            except Exception:
                state["work_page"] = 0
            render_flow(chat_id, state)
            return
        else:
            return

        # Results flow: do not continue into task-creation student picker.
        if state.get("mode") == "results":
            work_title = state.get("work_title_filter") or "все работы"
            work_link = ""
            if state.get("works") and state.get("work_title_filter"):
                for w in state.get("works") or []:
                    if w.get("title") == state.get("work_title_filter"):
                        work_link = w.get("link", "")
                        break

            if not work_link and state.get("works") and not state.get("work_title_filter"):
                # "all works": show aggregate class snapshot.
                students = collect_students_from_works(state.get("works") or [], max_works=10, time_budget_sec=20)
                if not students:
                    students = collect_students_for_class(state.get("class_name") or "", state.get("teacher_ids"))
                lines = [f"<b>Результаты</b>", f"Класс: <b>{html.escape(state.get('class_name') or '')}</b>", ""]
                for idx, (nm, p) in enumerate(students[:40], start=1):
                    lines.append(f"{idx}. {html.escape(nm)} — <b>{p:.1f}%</b>")
                send_tg_message(chat_id, "\n".join(lines))
                return

            tasks, results_rows = fetch_testwork_results_page(work_link)
            class_label = html.escape(state.get("class_name") or "")
            work_label = html.escape(work_title)
            task_msg, students_msg = format_results_messages(tasks, results_rows, class_label, work_label)
            send_tg_message(chat_id, task_msg)
            send_tg_message(chat_id, students_msg)
            return

        # Give immediate UI feedback: collecting students can take time due to multiple Yaklass requests.
        try:
            w_label = state.get("work_title_filter") or "все работы"
            edit_tg_message(
                chat_id,
                message_id,
                "<b>Создание задачи</b>\n\n"
                "<i>Шаг 4/5</i>: загружаю список учеников...\n"
                f"Работа: <b>{html.escape(w_label)}</b>",
                inline_keyboard=[[{"text": "❌ Отмена", "callback_data": "flow:cancel"}]],
            )
        except Exception:
            pass

        # Load students in background to avoid blocking the bot for minutes.
        token = f"{time.time():.6f}:{random.randint(1000,9999)}"
        state["students_loading"] = token

        def _bg_load_students(chat_id_local, message_id_local, token_local):
            try:
                st = telegram_states.get(chat_id_local) or {}
                works_local = st.get("works") or []
                cls_local = st.get("class_name", "")
                teacher_ids_local = st.get("teacher_ids")

                students_local = []
                if st.get("work_title_filter"):
                    work_link_local = ""
                    for w in works_local:
                        if w.get("title") == st.get("work_title_filter"):
                            work_link_local = w.get("link", "")
                            break
                    if work_link_local:
                        students_local = collect_students_for_work(work_link_local)
                else:
                    students_local = collect_students_from_works(works_local, max_works=10, time_budget_sec=20)
                    if not students_local:
                        students_local = collect_students_for_class(cls_local, teacher_ids_local)

                st2 = telegram_states.get(chat_id_local)
                if not st2 or st2.get("students_loading") != token_local:
                    return
                if not students_local:
                    edit_tg_message(
                        chat_id_local,
                        message_id_local,
                        "<b>Создание задачи</b>\n\n"
                        "<i>Шаг 4/5</i>: не удалось загрузить учеников.\n"
                        "Попробуйте выбрать другую работу или нажмите «Назад».",
                        inline_keyboard=[
                            [{"text": "⬅️ Назад", "callback_data": "work:back"}],
                            [{"text": "❌ Отмена", "callback_data": "flow:cancel"}],
                        ],
                    )
                    return

                st2["students"] = students_local
                st2["step"] = "pick_student_btn"
                st2["student_page"] = 0
                render_flow(chat_id_local, st2)
            except Exception as e:
                try:
                    print(f"[Flow] load students failed: {e}")
                except Exception:
                    pass

        threading.Thread(target=_bg_load_students, args=(chat_id, message_id, token), daemon=True).start()
        return

        state["students"] = students
        state["step"] = "pick_student_btn"
        state["student_page"] = 0
        render_flow(chat_id, state)
        return

    # Students
    if data.startswith("stud:"):
        _ack()
        if state.get("mode") == "results":
            # In results flow student selection is not used for job creation.
            return
        if data == "stud:back":
            state["step"] = "pick_work_btn"
            render_flow(chat_id, state)
            return
        if state.get("step") != "pick_student_btn":
            return
        parts = data.split(":")
        if len(parts) >= 3 and parts[1] == "t":
            try:
                idx = int(parts[2])
                state["student_name"] = (state.get("students") or [])[idx][0]
            except Exception:
                return
            if state.get("task_mode") == "solve":
                # Создаём задачу сразу, без запроса целевого процента
                job_id, err = create_job(
                    class_name=state["class_name"],
                    student_name=state["student_name"],
                    target_percentage=int(state.get("pct_max", 0)),
                    teacher_ids=state["teacher_ids"],
                    work_title_filter=state.get("work_title_filter", ""),
                    chat_id=chat_id,
                    mode="solve",
                    pct_min=state.get("pct_min", 0),
                    pct_max=state.get("pct_max", 0),
                    delay_min_sec=state.get("delay_min_sec", 60),
                    delay_max_sec=state.get("delay_max_sec", 180),
                    monitor_new_tasks=bool(not state.get("work_title_filter")),
                )
                telegram_states.pop(chat_id, None)
                if err:
                    send_tg_message(chat_id, err)
                else:
                    send_tg_message(
                        chat_id,
                        f"Готово. Задача #{job_id} (режим решать) создана.",
                        keyboard=main_menu_keyboard(is_admin(chat_id)),
                    )
            else:
                state["step"] = "pick_target"
                render_flow(chat_id, state)
            return
        if len(parts) >= 3 and parts[1] == "p":
            try:
                state["student_page"] = int(parts[2])
            except Exception:
                state["student_page"] = 0
            render_flow(chat_id, state)
            return
        return

    _ack()


def is_allowed_chat(chat_id):
    return str(chat_id) in approved_chat_ids


def register_access_request(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id:
        return
    user = msg.get("from", {}) or {}
    if chat_id in pending_access_requests:
        return
    pending_access_requests[chat_id] = {
        "requested_at": now_str(),
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "username": user.get("username", ""),
    }
    save_access_state()
    if chat_id != ADMIN_CHAT_ID:
        send_tg_message(
            ADMIN_CHAT_ID,
            "Новая заявка на доступ:\n"
            f"id: {chat_id}\n"
            f"name: {user.get('first_name', '')} {user.get('last_name', '')}\n"
            f"@{user.get('username', '')}\n"
            "Команда: <code>approve &lt;id&gt;</code> или <code>reject &lt;id&gt;</code>",
        )


def pending_requests_text():
    if not pending_access_requests:
        return "Заявок нет."
    lines = ["Заявки на доступ:"]
    for chat_id, data in pending_access_requests.items():
        lines.append(
            f"{chat_id} | {data.get('first_name', '')} {data.get('last_name', '')} "
            f"| @{data.get('username', '')} | {data.get('requested_at', '')}"
        )
    return "\n".join(lines)


def approve_chat(chat_id_value):
    chat_id = str(chat_id_value).strip()
    approved_chat_ids.add(chat_id)
    pending_access_requests.pop(chat_id, None)
    save_access_state()
    send_tg_message(chat_id, "Доступ к боту одобрен. Отправьте /start.")


def reject_chat(chat_id_value):
    chat_id = str(chat_id_value).strip()
    pending_access_requests.pop(chat_id, None)
    save_access_state()
    send_tg_message(chat_id, "Запрос доступа отклонен.")


def start_create_flow(chat_id):
    teachers, classes = fetch_filter_options()
    if not teachers:
        send_tg_message(chat_id, "Не удалось получить список учителей.")
        return

    telegram_states[chat_id] = {
        "mode": "create",
        "step": "pick_mode_btn",
        "task_mode": None,
        "teachers": teachers,
        # Will be refreshed after teacher selection (teacher might not have some classes).
        "classes": [],
        "teacher_selected": set(),
        "teacher_all": False,
        "teacher_page": 0,
        "class_page": 0,
        "work_page": 0,
        "student_page": 0,
        "pct_min": 0,
        "pct_max": 0,
        "delay_min_sec": 60,
        "delay_max_sec": 180,
    }

    render_flow(chat_id, telegram_states[chat_id], force_send=True)


def start_results_flow(chat_id):
    teachers, classes = fetch_filter_options()
    if not teachers:
        send_tg_message(chat_id, "Не удалось получить список учителей.")
        return
    telegram_states[chat_id] = {
        "mode": "results",
        "step": "pick_teacher_btn",
        "teachers": teachers,
        "classes": [],
        "teacher_selected": set(),
        "teacher_all": False,
        "teacher_page": 0,
        "class_page": 0,
        "work_page": 0,
        "student_page": 0,
    }
    state = telegram_states[chat_id]
    text = (
        "<b>Результаты</b>\n\n"
        "Выберите учителей (можно несколько), затем класс и работу.\n"
        "После выбора работы бот покажет таблицу результатов."
    )
    resp = send_tg_message(chat_id, text, inline_keyboard=build_teacher_keyboard(state))
    try:
        state["flow_msg_id"] = int(resp.get("result", {}).get("message_id"))
    except Exception:
        state["flow_msg_id"] = None


def handle_create_flow(chat_id, text):
    state = telegram_states.get(chat_id)
    if not state:
        return False
    raw, lower = normalize_user_input(text)
    if lower in ("cancel", "/cancel", "отмена"):
        telegram_states.pop(chat_id, None)
        send_tg_message(chat_id, "Создание задачи отменено.", keyboard=main_menu_keyboard(is_admin(chat_id)))
        return True

    # Button-driven flow uses callback queries for steps 1-4.
    if state.get("step", "").endswith("_btn"):
        send_tg_message(chat_id, "Используйте кнопки под сообщением. Для выхода нажмите ❌ Отмена.")
        return True

    # solve mode: percent range
    if state.get("step") == "pct_range":
        try:
            parts = re.split(r"[-–]", raw)
            lo, hi = float(parts[0]), float(parts[1])
            lo, hi = sorted((lo, hi))
            if lo < 0 or hi > 100:
                raise ValueError()
        except Exception:
            send_tg_message(chat_id, "Формат: 30-55 (проценты от 0 до 100).")
            return True
        state["pct_min"] = lo
        state["pct_max"] = hi
        state["step"] = "delay_range"
        send_tg_message(chat_id, "Шаг: диапазон времени (сек). Введите два числа, пример: 120-420.")
        return True

    if state.get("step") == "delay_range":
        try:
            parts = re.split(r"[-–]", raw)
            lo, hi = int(float(parts[0])), int(float(parts[1]))
            lo, hi = sorted((lo, hi))
            if lo < 10 or hi < lo:
                raise ValueError()
        except Exception:
            send_tg_message(chat_id, "Формат: 120-420 (секунды, минимум 10).")
            return True
        state["delay_min_sec"] = lo
        state["delay_max_sec"] = hi
        state["step"] = "pick_teacher_btn"
        render_flow(chat_id, state, force_send=True)
        return True

    if state["step"] == "pick_teacher":
        teachers = state.get("teachers") or []
        if raw.strip() == "0":
            teacher_ids = [""]
            teacher_map = {"": "все учителя"}
            teacher_name = "все учителя"
        else:
            try:
                idxs = []
                for part in raw.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    idxs.append(int(part) - 1)
                idxs = sorted(set(idxs))
            except Exception:
                send_tg_message(chat_id, "Нужны номера учителей через запятую (пример: 1,3,5) или 0.")
                return True

            teacher_ids = []
            teacher_names = []
            teacher_map = {}
            for idx in idxs:
                if idx < 0 or idx >= len(teachers):
                    continue
                t_id, t_name = teachers[idx]
                teacher_ids.append(t_id)
                teacher_names.append(t_name)
                teacher_map[t_id] = t_name

            if not teacher_ids:
                send_tg_message(chat_id, "Нужен номер учителя из списка (или 0).")
                return True

            teacher_name = ", ".join(teacher_names)

        if not state.get("classes"):
            send_tg_message(chat_id, "Не удалось получить классы.")
            telegram_states.pop(chat_id, None)
            return True

        state["teacher_ids"] = teacher_ids
        state["teacher_map"] = teacher_map
        state["teacher_name"] = teacher_name
        state["step"] = "pick_class"
        lines = [f"Учитель: {teacher_name}", "", "Шаг 2/5: выберите класс (номер):"]
        for i, c_item in enumerate(state["classes"], start=1):
            lines.append(f"{i}. {c_item[1]}")
        send_tg_message(chat_id, "\n".join(lines))
        return True

    if state["step"] == "pick_class":
        try:
            idx = int(raw) - 1
            class_id, class_label = state["classes"][idx]
        except Exception:
            send_tg_message(chat_id, "Нужен номер класса из списка.")
            return True

        teacher_ids = state.get("teacher_ids") or [""]
        works = []
        # If "all teachers" selected, a single fetch is enough.
        if teacher_ids == [""]:
            works = fetch_works_list("", class_id)
        else:
            for t_id in teacher_ids:
                try:
                    w = fetch_works_list(t_id, class_id)
                    for item in w:
                        item["teacher_id"] = t_id
                    works.extend(w)
                except Exception:
                    continue

            # De-dup by link if present; otherwise by title+class.
            seen = set()
            unique = []
            for w in works:
                key = w.get("link") or (w.get("title"), w.get("class_name"), w.get("teacher_id"))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(w)
            works = unique

        state["class_id"] = class_id
        state["class_name"] = works[0]["class_name"] if works else class_label
        state["works"] = works
        state["step"] = "pick_work"
        lines = [f"Класс: {class_label}", "", "Шаг 3/5: выберите работу:", "0. Все работы"]
        show_teacher = bool(state.get("teacher_ids")) and state.get("teacher_ids") not in ([""], None) and len(state.get("teacher_ids")) > 1
        teacher_map = state.get("teacher_map") or {}
        for i, work in enumerate(works[:40], start=1):
            status = work["status"]
            if show_teacher:
                t_label = teacher_map.get(work.get("teacher_id"), work.get("teacher_id", "")).strip()
                t_prefix = f"({t_label}) " if t_label else ""
                lines.append(f"{i}. [{status}] {t_prefix}{work['title']}")
            else:
                lines.append(f"{i}. [{status}] {work['title']}")
        send_tg_message(chat_id, "\n".join(lines))
        return True

    if state["step"] == "pick_work":
        if raw == "0":
            state["work_title_filter"] = ""
        else:
            try:
                idx = int(raw) - 1
                state["work_title_filter"] = state["works"][idx]["title"]
            except Exception:
                send_tg_message(chat_id, "Нужен номер работы из списка (или 0).")
                return True

        students = collect_students_for_class(state["class_name"], state["teacher_ids"])
        if not students:
            send_tg_message(chat_id, "В этом классе пока нет доступных учеников.")
            telegram_states.pop(chat_id, None)
            return True

        state["step"] = "pick_student"
        state["students"] = students

        w_text = state["work_title_filter"] if state["work_title_filter"] else "все работы"
        lines = [f"Класс: {state['class_name']}", f"Работа: {w_text}", "", "Шаг 4/5: выберите ученика (номер):"]
        for idx, item in enumerate(students, start=1):
            lines.append(f"{idx}. {item[0]} ({item[1]:.1f}%)")
        send_tg_message(chat_id, "\n".join(lines))
        return True

    if state["step"] == "pick_student":
        try:
            idx = int(raw) - 1
            student_name = state["students"][idx][0]
        except Exception:
            send_tg_message(chat_id, "Нужен номер ученика из списка.")
            return True

        state["student_name"] = student_name
        if state.get("task_mode") == "solve":
            # Создаём задачу сразу, используя заданный диапазон.
            job_id, err = create_job(
                class_name=state["class_name"],
                student_name=state["student_name"],
                target_percentage=int(state.get("pct_max", 0)),
                teacher_ids=state["teacher_ids"],
                work_title_filter=state.get("work_title_filter", ""),
                chat_id=chat_id,
                mode="solve",
                pct_min=state.get("pct_min", 0),
                pct_max=state.get("pct_max", 0),
                delay_min_sec=state.get("delay_min_sec", 60),
                delay_max_sec=state.get("delay_max_sec", 180),
                monitor_new_tasks=bool(not state.get("work_title_filter")),
            )
            telegram_states.pop(chat_id, None)
            if err:
                send_tg_message(chat_id, err)
            else:
                send_tg_message(
                    chat_id,
                    f"Готово. Задача #{job_id} (режим решать) создана.",
                    keyboard=main_menu_keyboard(is_admin(chat_id)),
                )
            return True

        state["step"] = "pick_target"
        send_tg_message(chat_id, f"Ученик: {student_name}\n\nШаг 5/5: введите целевой процент (0-100):")
        return True

    if state["step"] == "pick_target":
        try:
            target = int(raw)
            if target < 0 or target > 100:
                raise ValueError()
        except Exception:
            send_tg_message(chat_id, "Введите целое число от 0 до 100.")
            return True

        job_id, err = create_job(
            class_name=state["class_name"],
            student_name=state["student_name"],
            target_percentage=target,
            teacher_ids=state["teacher_ids"],
            work_title_filter=state.get("work_title_filter", ""),
            chat_id=chat_id,
            mode=state.get("task_mode", "sabotage"),
        )
        telegram_states.pop(chat_id, None)
        if err:
            send_tg_message(chat_id, err)
        else:
            send_tg_message(
                chat_id,
                f"Готово. Задача #{job_id} создана.",
                keyboard=main_menu_keyboard(is_admin(chat_id)),
            )
        return True

    return False


def handle_telegram_message(chat_id, text):
    _, lower = normalize_user_input(text)
    admin = is_admin(chat_id)

    # Явные команды должны сбрасывать любой незавершённый flow.
    if lower in ("new", "новая задача"):
        telegram_states.pop(chat_id, None)
        start_create_flow(chat_id)
        return

    if lower in ("results", "результаты"):
        telegram_states.pop(chat_id, None)
        start_results_flow(chat_id)
        return

    if lower in ("/start", "menu", "меню"):
        telegram_states.pop(chat_id, None)
        send_main_menu(chat_id)
        return

    if handle_create_flow(chat_id, text.strip()):
        return

    if lower in ("requests", "заявки"):
        if not admin:
            send_tg_message(chat_id, "Команда доступна только администратору.")
            return
        send_tg_message(chat_id, pending_requests_text(), parse_mode=None)
        return

    if lower.startswith("approve ") or lower.startswith("принять "):
        if not admin:
            send_tg_message(chat_id, "Команда доступна только администратору.")
            return
        parts = text.strip().split()
        if len(parts) < 2:
            send_tg_message(chat_id, "Формат: <code>approve &lt;chat_id&gt;</code>")
            return
        approve_chat(parts[1])
        send_tg_message(chat_id, f"Чат {parts[1]} одобрен.")
        return

    if lower.startswith("reject ") or lower.startswith("отклонить "):
        if not admin:
            send_tg_message(chat_id, "Команда доступна только администратору.")
            return
        parts = text.strip().split()
        if len(parts) < 2:
            send_tg_message(chat_id, "Формат: <code>reject &lt;chat_id&gt;</code>")
            return
        reject_chat(parts[1])
        send_tg_message(chat_id, f"Чат {parts[1]} отклонен.")
        return

    if lower in ("active", "активные задачи"):
        send_active_jobs_view(chat_id, mode="view")
        return

    if lower in ("history", "история"):
        send_tg_message(chat_id, history_text(), parse_mode=None)
        return

    if lower in ("percent", "проценты"):
        send_active_jobs_view(chat_id, mode="view")
        return

    if lower == "logs_help":
        send_active_jobs_view(chat_id, mode="logs")
        return

    if lower == "stop_help":
        send_active_jobs_view(chat_id, mode="stop")
        return

    if lower == "delhist_help":
        send_active_jobs_view(chat_id, mode="delhist")
        return

    if lower == "clearhist":
        ok, msg = clear_history(chat_id)
        send_tg_message(chat_id, msg)
        return

    send_tg_message(chat_id, "Неизвестная команда. Нажмите кнопку 'ℹ️ Меню' или введите /start")


def run_telegram_bot():
    print("[TG] Бот запущен")
    offset = None
    backoff_sec = 3
    while True:
        try:
            payload = {"timeout": 45}
            if offset is not None:
                payload["offset"] = offset

            data = tg_request("getUpdates", payload=payload, is_get=True)
            if not data or not data.get("ok"):
                time.sleep(backoff_sec)
                backoff_sec = min(backoff_sec * 2, 60)
                continue
            backoff_sec = 3

            updates = data.get("result", [])
            for upd in updates:
                try:
                    offset = upd["update_id"] + 1

                    cbq = upd.get("callback_query")
                    if cbq:
                        cb_id = cbq.get("id")
                        cb_data = cbq.get("data", "")
                        msg = cbq.get("message") or {}
                        chat_id = (msg.get("chat") or {}).get("id")
                        message_id = msg.get("message_id")
                        if chat_id and message_id and cb_data:
                            handle_callback_query(chat_id, message_id, cb_data, callback_query_id=cb_id)
                        else:
                            if cb_id:
                                answer_callback(cb_id)
                        continue

                    msg = upd.get("message")
                    if not msg:
                        continue

                    chat_id = msg.get("chat", {}).get("id")
                    text = msg.get("text", "")
                    if not chat_id or not text:
                        continue

                    if not is_allowed_chat(chat_id):
                        register_access_request(msg)
                        send_tg_message(
                            chat_id,
                            "Доступ закрыт. Заявка отправлена администратору. Ожидайте одобрения.",
                            parse_mode=None,
                        )
                        continue

                    handle_telegram_message(chat_id, text)
                except Exception as e:
                    print(f"[TG] Ошибка обработки update: {e}")
        except Exception as e:
            print(f"[TG] Ошибка цикла getUpdates: {e}")
            time.sleep(backoff_sec)
            backoff_sec = min(backoff_sec * 2, 60)


def run_cli_mode():
    t_ids = select_teacher()
    t_class = input("Класс (точное имя): ").strip()
    t_name = input("Имя ученика: ").strip()
    t_pct_str = input("Целевой процент (0-100): ").strip()
    try:
        t_pct = int(t_pct_str)
    except Exception:
        t_pct = 0

    job_id, err = create_job(t_class, t_name, t_pct, t_ids)
    if err:
        print(err)
        return

    print(f"Запущена задача #{job_id}. Ctrl+C для выхода.")
    while True:
        time.sleep(10)


if __name__ == "__main__":
    ensure_runtime_files()
    load_access_state()
    init_job_counter_from_history()
    ensure_persist_thread_started()
    import_cookies_from_file()
    if not login():
        raise SystemExit(1)
    threading.Thread(target=periodic_relogin_loop, kwargs={"interval_seconds": 1800}, daemon=True).start()

    restored = restore_active_jobs_from_disk()
    if restored:
        # Notify chats once per restart (no per-job spam).
        try:
            chat_ids = set()
            with jobs_lock:
                for j in active_jobs.values():
                    cid = j.get("chat_id")
                    if cid:
                        chat_ids.add(str(cid))
            for cid in sorted(chat_ids):
                send_tg_message(cid, f"Восстановлено активных задач после перезапуска: {restored}", parse_mode=None)
        except Exception:
            pass

    if TELEGRAM_BOT_TOKEN:
        run_telegram_bot()
    else:
        run_cli_mode()
