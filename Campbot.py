import requests
import time
import random
import concurrent.futures as futures
import itertools
import os
from typing import List, Dict, Optional, Tuple
from threading import Lock

# -----------------------------  配置区域  ------------------------------------ #
CLIENT_KEY = os.getenv("YESCAPTCHA_KEY", "7ddecc3d151bf99d97f7cd702b2d94082af78dcc56492")
WEBSITE_URL = "https://faucet.campnetwork.xyz/"
WEBSITE_KEY = "5b86452e-488a-4f62-bd32-a332445e2f51"
MAX_WORKERS = 3
POLL_INTERVAL = 3
CAPTCHA_TIMEOUT = 180

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

# ------------------------  文件读取辅助函数  ---------------------------------- #
def _load_lines(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

ADDRESSES: List[str] = _load_lines("addresses.txt") or []
PROXY_URLS: List[str] = _load_lines("proxies.txt") or []

# ------------------------  失败地址记录区  ---------------------------------- #
FAILED_ADDRESSES: List[Tuple[str, str]] = []
FAILED_ADDRESSES_LOCK = Lock()

# --------------------  与 YesCaptcha 交互的函数  ---------------------------- #
def create_task(client_key: str, website_url: str, website_key: str, user_agent: str) -> Optional[int]:
    url = "https://api.yescaptcha.com/createTask"
    payload = {
        "clientKey": client_key,
        "task": {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
            "userAgent": user_agent,
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        data = resp.json()
        if data.get("errorId") == 0:
            return data.get("taskId")
        print("[create_task] 错误:", data.get("errorDescription"))
    except Exception as e:
        print("[create_task] 异常:", e)
    return None

def get_result(client_key: str, task_id: int, *, timeout: int = CAPTCHA_TIMEOUT, poll: int = POLL_INTERVAL) -> Optional[str]:
    url = "https://api.yescaptcha.com/getTaskResult"
    payload = {"clientKey": client_key, "taskId": task_id}
    start = time.time()
    while True:
        try:
            resp = requests.post(url, json=payload, timeout=60).json()
            if resp.get("errorId") != 0:
                print("[get_result] 错误:", resp.get("errorDescription"))
                return None
            if resp.get("status") == "ready":
                return resp["solution"]["gRecaptchaResponse"]
        except Exception as e:
            print("[get_result] 异常:", e)
            return None
        if time.time() - start > timeout:
            print(f"[get_result] 超时（{timeout}s）")
            return None
        time.sleep(poll)

def claim(
    address: str,
    hcaptcha_response: str,
    user_agent: str,
    proxy_url: Optional[str] = None,
    retries: int = 3,
) -> Tuple[Optional[int], str]:
    url = "https://faucet-go-production.up.railway.app/api/claim"
    headers = {
        "h-captcha-response": hcaptcha_response,
        "user-agent": user_agent,
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://faucet.campnetwork.xyz",
        "referer": "https://faucet.campnetwork.xyz/",
    }
    payload = {"address": address}

    proxies: Optional[Dict[str, str]] = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, proxies=proxies, timeout=60)
            return resp.status_code, resp.text
        except Exception as e:
            print(f"[claim] 第 {attempt} 次请求异常（{address}）:", e)
            time.sleep(3)
    return None, "请求多次失败"

# ---------------------------  线程工作函数  ---------------------------------- #
def worker(task: Tuple[str, str]) -> None:
    address, proxy_url = task
    user_agent = random.choice(USER_AGENTS)

    print(f"\n--- 开始处理地址 {address} 使用代理 {proxy_url} ---")

    print(f"[Step 1] 正在为地址 {address[:10]}... 创建验证码任务...")
    task_id = create_task(CLIENT_KEY, WEBSITE_URL, WEBSITE_KEY, user_agent)
    if not task_id:
        print(f"[Step 1] ❌ 创建验证码任务失败，跳过 {address}")
        with FAILED_ADDRESSES_LOCK:
            FAILED_ADDRESSES.append((address, "创建验证码任务失败"))
        return
    print(f"[Step 1] ✅ 创建成功，task_id = {task_id}")

    print(f"[Step 2] 正在轮询验证码结果...")
    hcaptcha_response = get_result(CLIENT_KEY, task_id)
    if not hcaptcha_response:
        print(f"[Step 2] ❌ 获取验证码结果失败，跳过 {address}")
        with FAILED_ADDRESSES_LOCK:
            FAILED_ADDRESSES.append((address, "获取验证码失败"))
        return
    print(f"[Step 2] ✅ 获取成功，hcaptcha_response 长度: {len(hcaptcha_response)}")

    print(f"[Step 3] 正在提交领取请求...")
    status_code, response_text = claim(address, hcaptcha_response, user_agent, proxy_url)
    print(f"[Step 3] 🎉 地址 {address} → 状态码 {status_code}, 返回内容前 200 字：\n{response_text[:200]}")

    if status_code != 200:
        try:
            data = requests.utils.json.loads(response_text)
            reason = data.get("error") or data.get("message") or str(data)
        except Exception:
            reason = response_text[:200].replace("\n", "\\n")
        with FAILED_ADDRESSES_LOCK:
            FAILED_ADDRESSES.append((address, f"{status_code} - {reason}"))

# --------------------------------  主函数  ----------------------------------- #
def main() -> None:
    if not ADDRESSES:
        raise SystemExit("❌ 未找到地址，请在 addresses.txt 中添加或在代码中配置 ADDRESSES 列表。")
    if not PROXY_URLS:
        print("⚠️ 未提供代理，将直接使用本机网络发送请求（可能触发风控）。")

    tasks = list(zip(ADDRESSES, itertools.cycle(PROXY_URLS or [""])))
    print(f"准备为 {len(tasks)} 个地址领取水龙头，每次最多并发 {MAX_WORKERS} 个线程…\n")

    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(worker, tasks)

    print("\n✅ 全部领取流程已完成。")
    if FAILED_ADDRESSES:
        print("\n❌ 以下地址领取失败（包含失败原因）：")
        with open("failed_addresses.txt", "w", encoding="utf-8") as f:
            for addr, reason in FAILED_ADDRESSES:
                print(f"- {addr} → 原因: {reason}")
                f.write(f"{addr} → {reason}\n")
        print("\n📁 已将失败地址与原因保存到 failed_addresses.txt")
    else:
        print("\n🎉 所有地址均成功领取！")

if __name__ == "__main__":
    main()
