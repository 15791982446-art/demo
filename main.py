import requests
import yaml
import random
from datetime import datetime, timedelta
import json
import os
import logging

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from urllib.parse import urlsplit, unquote
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
import time


logging.basicConfig(
                    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S',
                    level=logging.INFO)

WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
BOOK_DAYS_AHEAD = 2  # 预约后天
ALREADY_BOOKED_KEYS = ("已有预约", "重复预约", "请先取消", "当前已有", "只能预约一个", "已存在预约")
SEAT_TAKEN_KEYS = ("已被", "占用", "不可用", "不存在", "冲突")


def target_weekday_name():
    return WEEKDAYS[(datetime.now().weekday() + BOOK_DAYS_AHEAD) % 7]


def is_booking_enable(date_cfg):
    if date_cfg['启用']:
        return True
    return False


def expand_seat_cfg(cfg):
    ids = []
    for begin, end in cfg['ranges']:
        ids.extend(range(begin, end + 1))
    return ids


def get_seats_with_config(user_config, date_config, seat_config):
    seat_name = date_config['name']
    if seat_name == "自定义":
        return user_config['自定义']
    seats = expand_seat_cfg(seat_config[seat_name])
    # 先按「自定义」里指定的座位顺序试，全被占了再扩到整个房间
    preferred = [s for s in user_config.get('自定义', []) if s in seats]
    return preferred + [s for s in seats if s not in preferred]


def slot_label(start, hours):
    return "{:g}:00-{:g}:00".format(start, start + hours)


def is_slot_ok(code, message):
    # 服务器成功时 CODE 返回字符串 "ok"，不是数字 200
    return (str(code).lower() == "ok" or code == 200
            or "预约成功" in message or any(k in message for k in ALREADY_BOOKED_KEYS))


# 图书馆晚上放号，提前跑起来就等到点再抢；已经过点就直接开抢
def wait_until_release(release_time):
    try:
        hh, mm, ss = [int(x) for x in str(release_time).split(":")]
    except Exception:
        hh, mm, ss = 20, 0, 0
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if now >= target:
        print("放号时间 {:02d}:{:02d} 已过，立即开始".format(hh, mm))
        return
    wait_seconds = (target - now).total_seconds()
    print("等待 {:02d}:{:02d}:{:02d} 放号，还需 {:.0f} 秒".format(hh, mm, ss, wait_seconds))
    time.sleep(wait_seconds)
    print("放号时间到，开抢")


# 一天要么配 开始时间/持续小时数 一个时段，要么配「时间段」列表约多段
def get_time_slots(date_config):
    if '时间段' in date_config:
        if '开始时间' in date_config or '持续小时数' in date_config:
            raise ValueError("「时间段」与「开始时间/持续小时数」不能同时配置，请只保留一种")
        if not date_config['时间段']:
            raise ValueError("「时间段」为空，请至少填一个时段")
        slots = [(item['开始时间'], item['持续小时数']) for item in date_config['时间段']]
    else:
        slots = [(date_config['开始时间'], date_config['持续小时数'])]
    for _, hours in slots:
        if hours <= 0:
            raise ValueError("持续小时数必须大于 0")
    slots = sorted(slots)
    for (prev_start, prev_hours), (next_start, _) in zip(slots, slots[1:]):
        if next_start < prev_start + prev_hours:
            raise ValueError("时间段重合：{} 与 {}:00 起".format(
                slot_label(prev_start, prev_hours), next_start))
    return slots


class SeatAutoBooker:
    def __init__(self, booker_config):
        self.json = None
        self.resp = None
        self.user_data = None

        logging.info('Creating SeatAutoBooker object')

        self.un = os.environ["SCHOOL_ID"].strip()  # 学号
        print("使用用户：{}".format(self.un))
        self.pd = os.environ["PASSWORD"].strip()  # 密码
        self.SCKey = None
        try:
            self.SCKey = os.environ["SCKEY"].strip()
        except KeyError:
            print("没有Server酱的key,将不会推送消息")

        chrome_options = Options()
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "")
        if not chromedriver_path:
            for candidate in ('/usr/local/bin/chromedriver', '/opt/homebrew/bin/chromedriver'):
                if os.path.exists(candidate):
                    chromedriver_path = candidate
                    break
        service = Service(chromedriver_path) if chromedriver_path else Service()
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.wait = WebDriverWait(self.driver, 10, 0.5)
        self.cookie = None

        self.cfg = booker_config

    def book_begin_timestamp(self, start):
        today_0_clock = datetime.strptime(datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S")
        book_time = today_0_clock + timedelta(days=BOOK_DAYS_AHEAD) + timedelta(hours=start)
        delta = book_time - self.cfg["start-time"]
        return int(delta.days * 24 * 3600 + delta.seconds)

    def prepare_booking_seat_map(self, user_config, seat_config, start, hours):
        """对应人工流程里「选日期 -> 加载座位图」这一步，预约前必须先做"""
        date_config = user_config[target_weekday_name()]
        floor = date_config['name']
        room_title = seat_config.get(floor, {}).get('title', floor)
        headers = dict(self.cfg["headers"])
        headers['Cookie'] = self.cookie
        try:
            r = requests.get("https://hdu.huitu.zhishulib.com/Space/Category/list?LAB_JSON=1",
                             headers=headers)
            data = r.json()
            items = data["content"]["children"][1]["defaultItems"]
            query = None
            names = []
            for item in items:
                name = item.get("name", "")
                names.append(name)
                if query is None and (name == room_title or name == floor
                                      or floor in name or (room_title and room_title in name)):
                    link = unquote(item.get("link", {}).get("url", ""))
                    query = link.split("?", 1)[1] if "?" in link else ""
            if query is None:
                print("加载座位图：没找到房间「{}」，可选项 {}".format(room_title, " / ".join(names)))
                return False
            params = {}
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = unquote(v)
            params["beginTime"] = str(self.book_begin_timestamp(start))
            params["duration"] = str(3600 * hours)
            params["num"] = "1"
            r2 = requests.post("https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1",
                               data=params, headers=headers)
            print("座位图已加载：HTTP {}  {}".format(r2.status_code, r2.text[:100].replace("\n", " ")))
            return True
        except Exception as e:
            print("加载座位图失败：{} {}".format(e.__class__.__name__, e))
            return False

    def book_favorite_seat(self, user_config, seat_config):
        results = []
        preferred_seat = None
        for start, hours in get_time_slots(user_config[target_weekday_name()]):
            label = slot_label(start, hours)
            print("开始预约时段 {}".format(label))
            self.prepare_booking_seat_map(user_config, seat_config, start, hours)
            result = self._book_slot(user_config, seat_config, start, hours, preferred_seat)
            if result:
                code, message, seat = result
                preferred_seat = seat
                results.append((label, code, message))
            else:
                results.append((label, -1, "预约失败"))
        return results

    def _book_slot(self, user_config, seat_config, start, hours, preferred_seat=None):
        max_retry = self.cfg["max-retry"]
        seat_attempts = 0   # 每次请求都 +1，用来轮换座位
        tried_times = 0     # 有效尝试次数，只有服务器给出明确结论才算
        error_times = 0     # 连续限流/异常次数，连续太多次才放弃
        while tried_times < max_retry and seat_attempts < max_retry * 5:
            try:
                result = self._book_favorite_seat(user_config, seat_config, start, hours, seat_attempts, preferred_seat)
                seat_attempts += 1
                msg = str(result[1]) if result else ""
                if result and any(k in msg for k in ALREADY_BOOKED_KEYS):
                    print("该时段已有预约，跳过：{}".format(result[1]))
                    return result
                if result and any(k in msg for k in ("频繁", "人数过多")):
                    # 限流不算一次有效尝试，短暂停顿后立刻再抢，别把放号窗口睡过去
                    error_times += 1
                    if error_times > max_retry:
                        print("连续被限流 {} 次，放弃本时段".format(error_times))
                        return result
                    wait = min(1 + error_times, 5)
                    print("被限流({})，{} 秒后重试（有效 {}/{}）".format(
                        result[1], wait, tried_times, max_retry))
                    time.sleep(wait)
                    continue
                if result and ("订座流程" in msg or "先查询" in msg):
                    # 服务器要求先查座位：重新查一次再约，不消耗有效次数
                    print("需要先查询座位({})，重新加载座位图后重试".format(result[1]))
                    self.prepare_booking_seat_map(user_config, seat_config, start, hours)
                    error_times += 1
                    if error_times > max_retry:
                        return result
                    time.sleep(1)
                    continue
                tried_times += 1
                error_times = 0
                if result and any(k in msg for k in SEAT_TAKEN_KEYS):
                    print("座位不可约({})，换一个，有效 {}/{}".format(
                        result[1], tried_times, max_retry))
                    time.sleep(0.5)
                    continue
                return result
            except Exception as e:
                # 抢号高峰服务器常常返回限流页而不是 JSON，解析会炸；
                # 这种不算一次有效尝试，停一下马上重试
                seat_attempts += 1
                error_times += 1
                if error_times > max_retry:
                    print("连续异常 {} 次，放弃本时段（{}）".format(error_times, e.__class__.__name__))
                    return None
                wait = min(1 + error_times, 5)
                print("{}，{} 秒后重试（响应无法解析，多半是限流）".format(e.__class__.__name__, wait))
                time.sleep(wait)
        return None

    def _book_favorite_seat(self, user_config, seat_config, start, hours, seat_attempts=0, preferred_seat=None):
        logging.info('Entering _book_favorite_seat method')
        date_config = user_config[target_weekday_name()]
        seats = get_seats_with_config(user_config, date_config, seat_config)
        today_0_clock = datetime.strptime(datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S")
        book_time = today_0_clock + timedelta(days=BOOK_DAYS_AHEAD) + timedelta(hours=start)
        delta = book_time - self.cfg["start-time"]
        total_seconds = delta.days * 24 * 3600 + delta.seconds
        # 第一轮优先沿用上一时段约到的座位，之后按顺序轮换，保证每次换一个新座位
        if seat_attempts == 0 and preferred_seat in seats:
            seat = preferred_seat
        else:
            seat = seats[seat_attempts % len(seats)]
        data = f"beginTime={total_seconds}&duration={3600 * hours}&&seats[0]={seat}&seatBookers[0]={self.user_data['uid']}"

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        print(data)
        self.resp = requests.post(self.cfg["target"], data=data, headers=headers)
        self.json = json.loads(self.resp.text)
        return self.json["CODE"], self.json["MESSAGE"] + " 座位:{}".format(seat), seat

    def _page_hint(self):
        """抓一段当前页面可见文字，登录失败时打进日志方便定位"""
        try:
            txt = self.driver.execute_script(
                "return (document.body ? document.body.innerText : '').replace(/[\\r\\n\\t ]+/g, ' ').slice(0, 200);")
            return txt or ""
        except Exception:
            return ""

    def login(self):
        logging.info('Login in')
        last_error = None
        # 放号高峰 SSO 会卡，失败就重来，最多 5 次
        for attempt in range(5):
            if attempt:
                print("登录重试第 {} 次".format(attempt))
                time.sleep(5)
            try:
                logging.info('开始登陆...')

                self.driver.get("https://hdu.huitu.zhishulib.com/")
                self.wait.until(lambda d: "sso.hdu.edu.cn" in d.current_url)

                # 排除同名隐藏域
                form_wait = WebDriverWait(self.driver, 30, 0.5)
                user_el = form_wait.until(EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "input[name='username']:not([type='hidden'])")
                ))

                # 关掉公告弹窗
                for close_button in self.driver.find_elements(
                    By.CSS_SELECTOR, "img.icon-close, .ant-modal-close"
                ):
                    if close_button.is_displayed() and close_button.is_enabled():
                        close_button.click()
                        self.wait.until(EC.invisibility_of_element(close_button))

                user_el.clear()
                user_el.send_keys(self.un)
                logging.info('输入用户名')

                pwd_el = form_wait.until(EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, "input[type='password']")))
                pwd_el.clear()
                pwd_el.send_keys(self.pd)
                logging.info('输入密码')

                # 手动补发 input/change 事件，让页面 JS 认为两个框都填好了，
                # 否则登录按钮会一直是 disabled（10-04 就是卡在这里）
                for el in (user_el, pwd_el):
                    self.driver.execute_script(
                        "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                        "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));", el)
                pwd_el.send_keys(Keys.TAB)

                login_btn = self.driver.find_element(By.CSS_SELECTOR, "button.login-button")
                try:
                    WebDriverWait(self.driver, 20, 0.5).until(
                        lambda d: "disabled" not in (login_btn.get_attribute("class") or ""))
                except Exception:
                    print("登录按钮仍然不可点，class={}  页面文字={}".format(
                        login_btn.get_attribute("class"), self._page_hint()))
                    # 兜底：直接派发点击事件
                    self.driver.execute_script("arguments[0].click();", login_btn)
                else:
                    login_btn.click()
                logging.info('点击登录按钮')

                # 放号时 SSO 很慢，给足 60 秒
                WebDriverWait(self.driver, 60, 0.5).until(
                    lambda d: urlsplit(d.current_url).hostname == "hdu.huitu.zhishulib.com")
                time.sleep(5)
                cookie_list = self.driver.get_cookies()
                self.cookie = ";".join([item["name"] + "=" + item["value"] + "" for item in cookie_list])
                self.cfg["headers"]['Cookie'] = self.cookie

                logging.info("登录成功！")
                return 0
            except Exception as e:
                last_error = e
                logging.error(f"登录失败：{e}")
                try:
                    print("登录失败，当前页面：{}  标题：{}  文字：{}".format(
                        self.driver.current_url, self.driver.title, self._page_hint()))
                except Exception:
                    pass
        logging.error(f"连续 5 次登录失败：{last_error}")
        return -1

    def get_user_info(self):
        logging.info('Getting user info')

        headers = self.cfg["headers"]
        headers['Cookie'] = self.cookie
        try:
            resp = requests.get("https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1",
                                headers=headers)
            self.user_data = resp.json()['DATA']
            _ = self.user_data['uid']
        except Exception as e:
            logging.exception(e)
            print(self.user_data)
            print(e.__class__.__name__ + ",获取用户数据失败")
            return -1
        print("获取用户数据成功")
        return 0

    def wechatNotice(self, title, desp=None):
        logging.info('Sending WeChat notice')

        if not self.SCKey:
            print("没有 Server酱 的 key，跳过微信通知")
            return False
        url = 'https://sctapi.ftqq.com/{0}.send'.format(self.SCKey)
        body = {
            'title': title,
            'desp': desp if desp else title,
        }
        try:
            r = requests.post(url, data=body, timeout=15)
            ret = r.json()
            data = ret.get('data') if isinstance(ret.get('data'), dict) else {}
            if ret.get('code') == 0 or data.get('error') == 'SUCCESS':
                print("Server酱通知成功")
                return True
            print("Server酱通知失败：{} {}".format(ret.get('message'), ret.get('code')))
            return False
        except Exception as e:
            logging.exception(e)
            print(e.__class__, "推送服务配置错误")
            return False

if __name__ == "__main__":
    logging.info('Start of the program')
    with open("user_config.yml", 'r') as f_obj:
        user_config = yaml.safe_load(f_obj)
    with open("config/basic_config.yml", 'r') as f_obj:
        basic_config = yaml.safe_load(f_obj)
    with open("config/seat_config.yml", 'r') as f_obj:
        seat_config = yaml.safe_load(f_obj)

    if not is_booking_enable(user_config[target_weekday_name()]):
        logging.info('预约未启用')
        print("预约未启用")
        exit(0)

    try:
        get_time_slots(user_config[target_weekday_name()])
    except (ValueError, KeyError, TypeError) as e:
        print("user_config.yml 中「{}」的时段配置有误：{}".format(target_weekday_name(), e))
        exit(-1)

    s = SeatAutoBooker(basic_config["SeatAutoBooker"])
    if not s.login() == 0:
        s.wechatNotice("预约失败：{} 登录失败".format(target_weekday_name()),
                       "登录环节出错，请到 GitHub Actions 查看日志。")
        s.driver.quit()
        logging.info('Login unsuccessful')
        exit(-1)
    if not s.get_user_info() == 0:
        s.wechatNotice("预约失败：{} 获取用户信息失败".format(target_weekday_name()),
                       "登录成功但拿不到用户数据，请到 GitHub Actions 查看日志。")
        s.driver.quit()
        logging.info('Getting user info unsuccessful')
        exit(-1)
    # 登录完再等放号，放号那一刻手里已经有登录态，直接开抢
    wait_until_release(s.cfg.get("release-time", "20:00:00"))
    # 20:00 整点系统会刷新座位数据，登录时查的那次作废，必须重新查一次座位
    if s.get_user_info() != 0:
        print("放号后重新查询座位失败，仍然继续尝试预约")
    results = s.book_favorite_seat(user_config=user_config, seat_config=seat_config)
    lines = []
    all_ok = True
    for slot, code, message in results:
        ok = is_slot_ok(code, message)
        all_ok = all_ok and ok
        print("预约结果[{}]: {} {}".format(slot, code, message))
        lines.append("{} {} {}".format("[成功]" if ok else "[失败]", slot, message))
    s.driver.quit()
    logging.info('End of the program')

    book_date = (datetime.now() + timedelta(days=BOOK_DAYS_AHEAD)).strftime("%m-%d")
    s.wechatNotice(
        "预约{}：{}（{}）".format("成功" if all_ok else "失败", target_weekday_name(), book_date),
        "\n\n".join(lines),
    )
    # 约失败要让 workflow 变红，别再假绿骗人
    if not all_ok:
        print("有时段未预约成功，退出码 1")
        exit(1)
