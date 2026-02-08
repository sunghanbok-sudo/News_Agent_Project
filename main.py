import requests
from bs4 import BeautifulSoup
from datetime import datetime
import json
import os
import sys

# Windows 콘솔 인코딩 문제 해결
sys.stdout.reconfigure(encoding='utf-8')

# --- 설정 및 상수 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "raw_news")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs", "reports")

# --- 에이전트 클래스 정의 ---

class NewsCollector:
    """
    [에이전트 1: 수집가]
    - 뉴스 데이터 수집 및 중복 제거
    - Raw Data 저장
    """
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0"}
    
    def collect(self, queries):
        print(f"🔎 [수집가] 뉴스 수집 시작: {queries}")
        all_news = []
        
        # 1. 네이버 뉴스 수집 시도
        for query in queries:
            try:
                naver_items = self._collect_naver(query)
                if naver_items:
                    all_news.extend(naver_items)
                else:
                    # 네이버 실패/차단 시 구글 뉴스(RSS) fallback
                    print(f"⚠️ [수집가] '{query}' 네이버 검색 결과 없음. 구글 뉴스로 대체합니다.")
                    google_items = self._collect_google_rss(query)
                    all_news.extend(google_items)
            except Exception as e:
                print(f"❌ [수집가] '{query}' 수집 중 에러: {e}")

        # 중복 제거 (URL 기준)
        seen_links = set()
        unique_news = []
        for n in all_news:
            if n['link'] not in seen_links:
                seen_links.add(n['link'])
                unique_news.append(n)

        self._save_raw_data(unique_news)
        return unique_news

    def _collect_naver(self, query):
        """네이버 뉴스 검색 (크롤링)"""
        items = []
        url = f"https://search.naver.com/search.naver?where=news&query={query}"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            if res.status_code != 200:
                return []
            
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # SDS 디자인 (list_news > li (bx))
            raw_items = soup.select("ul.list_news > li")
            if not raw_items:
               raw_items = soup.select(".news_area") # 구버전 fallback

            for art in raw_items:
                # 제목/링크
                title_node = art.select_one("a.news_tit")
                if not title_node:
                    title_node = art.select_one(".news_tit")
                
                if not title_node: continue
                
                title = title_node.text.strip()
                link = title_node['href']
                
                # 설명
                desc_node = art.select_one(".news_dsc")
                if not desc_node:
                    desc_node = art.select_one("div.news_dsc")
                    
                desc = desc_node.text.strip() if desc_node else ""
                
                items.append({
                    "title": title,
                    "link": link,
                    "desc": desc,
                    "source": "Naver",
                    "collected_at": datetime.now().isoformat(),
                    "query": query
                })
        except Exception:
            pass # 개별 실패는 무시하고 빈 리스트 리턴
        return items

    def _collect_google_rss(self, query):
        """구글 뉴스 RSS (Fallback용, 클라우드에서 안정적)"""
        items = []
        # 구글 뉴스 RSS URL (한국어 설정)
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                print(f"⚠️ [수집가] 구글 RSS 요청 실패: {res.status_code}")
                return []
                
            try:
                soup = BeautifulSoup(res.text, 'xml') # XML 파싱 시도 (lxml 필요)
            except Exception:
                soup = BeautifulSoup(res.text, 'html.parser') # lxml 없으면 내장 파서 사용

                soup = BeautifulSoup(res.text, 'html.parser') # lxml 없으면 내장 파서 사용

            xml_items = soup.find_all("item")
            if not xml_items:
                xml_items = soup.select("item") # backup
            
            # print(f"DEBUG: Found {len(xml_items)} items for {query}") 
            
            for item in xml_items:
                title = item.title.text if item.title else ""
                link = item.link.text if item.link else ""
                # RSS는 description에 HTML이 섞여있을 수 있음
                desc_html = item.description.text if item.description else ""
                desc_clean = BeautifulSoup(desc_html, "html.parser").text[:200]
                
                if title:
                    items.append({
                        "title": title,
                        "link": link,
                        "desc": desc_clean,
                        "source": "Google",
                        "collected_at": datetime.now().isoformat(),
                        "query": query
                    })
        except Exception as e:
            print(f"⚠️ [수집가] 구글 RSS 처리 중 오류: {e}")
        
        return items

    def _save_raw_data(self, news_list):
        if not os.path.exists(DATA_DIR):
            os.makedirs(DATA_DIR)
            
        today = datetime.now().strftime("%Y%m%d")
        filename = os.path.join(DATA_DIR, f"news_raw_{today}.json")
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(news_list, f, ensure_ascii=False, indent=4)
        print(f"💾 [수집가] 데이터 저장 완료: {len(news_list)}건 -> {filename}")


class NewsStrategist:
    """
    [에이전트 2: 전략 분석가]
    - 뉴스 가치 평가 및 점수 산정
    - 인사이트(기회/위기) 도출
    - 타겟: 50대 식품 마케팅 팀장
    """
    def __init__(self):
        # 1. 핵심 관심사 (Marketing & Biz)
        self.biz_keywords = ["마케팅", "캠페인", "콜라보", "팝업", "신제품", "매출", "해외 진출", "ESG", "M&A"]
        
        # 2. 산업 트렌드 (Industry Trends)
        self.trend_keywords = [
            "제로", "비건", "단백질", "헬시플레저", "가치소비", "푸드테크", "밀키트", "RMR", 
            "고령친화", "친환경", "숏폼", "유튜브"
        ]
        
        # 3. 위기/외부 요인 (Risk & External)
        self.risk_keywords = ["물가", "인플레이션", "환율", "원자재", "식중독", "이물질", "불매", "ASF"]
        
        # 4. 타겟 소비자 (Target Audience)
        self.target_keywords = ["MZ", "잘파", "1인가구", "시니어", "오피스"]

    def analyze(self, news_list):
        print("📊 [전략 분석가] 뉴스 분석 및 점수 산정 중...")
        analyzed_list = []
        
        for news in news_list:
            score = 0
            reasons = []
            
            title = news['title']
            desc = news['desc']
            content = title + " " + desc
            
            # 점수 산정 로직
            # 1. 트렌드 키워드 (가장 중요, 미래 먹거리) -> +10점
            for kw in self.trend_keywords:
                if kw in content:
                    score += 10
                    reasons.append(f"트렌드({kw})")

            # 2. 비즈니스/마케팅 키워드 (실무 연관) -> +5점
            for kw in self.biz_keywords:
                if kw in content:
                    score += 5
                    reasons.append(f"비즈니스({kw})")

            # 3. 위기 요인 (놓치면 안됨) -> +20점 (긴급)
            for kw in self.risk_keywords:
                if kw in content:
                    score += 20
                    reasons.append(f"🚨Risk({kw})")
                    news['is_critical'] = True

            # 4. 타겟 소비자 언급 -> +5점
            for kw in self.target_keywords:
                if kw in content:
                    score += 5
                    reasons.append(f"타겟({kw})")
            
            # 진주햄/육가공 직접 언급은 기본 점수 부여
            if "진주햄" in content or "육가공" in content:
                score += 10
                reasons.append("관심기업/산업")

            news['score'] = score
            news['reasons'] = ", ".join(list(set(reasons))) # 중복 제거
            
            # 인사이트 생성 (규칙 기반)
            if 'is_critical' in news:
                news['insight'] = "위기 요인 감지. 공급망 점검 및 리스크 대응 방안 수립 필요."
            elif score >= 20:
                news['insight'] = "업계 주요 트렌드와 전략이 결합된 기사. 신제품 기획 및 마케팅 전략에 벤치마킹 필요."
            elif score >= 10:
                news['insight'] = "시장 동향 파악을 위한 참고 자료. 경쟁사 움직임 주시 필요."
            else:
                news['insight'] = "일반적인 업계 소식."
                
            analyzed_list.append(news)
            
        # 점수 내림차순 정렬
        analyzed_list.sort(key=lambda x: x['score'], reverse=True)
        return analyzed_list


class NewsEditor:
    """
    [에이전트 3: 편집장]
    - 리포트 포맷팅 (가독성 최우선, 팀장님 보고용)
    - Top 10 선정
    """
    def create_report(self, analyzed_news):
        print("📝 [편집장] 데일리 인사이트 리포트 작성 중...")
        
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
            
        today_str = datetime.now().strftime("%Y-%m-%d")
        file_path = os.path.join(OUTPUT_DIR, f"Daily_Insight_Report_{today_str}.md")
        
        # 상위 10개 선정 (점수가 너무 낮은건 제외할 수도 있음)
        top_news = analyzed_news[:10]
        
        markdown_content = f"# ☕ [Marketing Brief] 식품업계 모닝 인사이트 ({today_str})\n\n"
        markdown_content += "> **Executive Summary**: 마케팅 팀장님을 위해 엄선한 오늘의 식품 산업 트렌드와 주요 이슈 10선입니다.\n\n"
        
        for i, news in enumerate(top_news):
            icon = "🚨" if news.get('is_critical') else "💡"
            
            # 제목에 링크 걸기
            markdown_content += f"### {i+1}. {icon} [{news['title']}]({news['link']})\n"
            markdown_content += f"- **Why This Matters**: {news['insight']}\n"
            markdown_content += f"- **Key Keywords**: {news['reasons']}\n"
            markdown_content += f"- **Summary**: {news['desc']}\n\n"
            markdown_content += "---\n"
            
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
            
        print(f"✅ [편집장] 리포트 발행 완료: {file_path}")
        return file_path

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

class NewsMessenger:
    """
    [에이전트 4: 메신저]
    - 이메일 발송 담당
    - GitHub Actions 등 환경변수에서 설정값 로드
    """
    def __init__(self):
        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 587
        self.email_user = os.environ.get("GMAIL_USER")
        self.email_password = os.environ.get("GMAIL_APP_PASSWORD")
        # 수신자는 발신자와 동일하게 설정하거나 별도 환경변수로 분리 가능
        self.email_to = os.environ.get("GMAIL_TO", self.email_user) 

    def send_report(self, report_path):
        print("📮 [메신저] 리포트 이메일 발송 준비...")
        
        if not self.email_user or not self.email_password:
            print("⚠️ [메신저] 이메일 설정(GMAIL_USER, GMAIL_APP_PASSWORD)이 없습니다. 발송을 건너뜁니다.")
            return

        try:
            # 리포트 내용 읽기
            with open(report_path, "r", encoding="utf-8") as f:
                report_content = f.read()

            # HTML 변환 (간단히)
            # 마크다운을 HTML로 변환하는 라이브러리(markdown)를 쓰면 좋지만, 
            # 여기서는 텍스트로 보조하거나 간단한 치환만 수행
            html_content = f"""
            <html>
            <body>
                <h2>📰 진주햄 데일리 인사이트</h2>
                <pre style="font-family: Malgun Gothic, sans-serif; white-space: pre-wrap;">{report_content}</pre>
                <hr>
                <p>본 메일은 News Agent에 의해 자동 발송되었습니다.</p>
            </body>
            </html>
            """

            msg = MIMEMultipart()
            msg['From'] = self.email_user
            msg['To'] = self.email_to
            msg['Subject'] = f"☕ [Marketing Brief] 식품업계 모닝 인사이트 ({datetime.now().strftime('%Y-%m-%d')})"
            
            msg.attach(MIMEText(html_content, 'html'))

            # SMTP 발송
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_user, self.email_password)
                server.send_message(msg)
            
            print(f"✅ [메신저] 이메일 발송 완료: {self.email_to}")
            
        except Exception as e:
            print(f"❌ [메신저] 이메일 발송 실패: {e}")

# --- 메인 실행부 ---
class NewsAgentSystem:
    def __init__(self):
        self.collector = NewsCollector()
        self.strategist = NewsStrategist()
        self.editor = NewsEditor()
        self.messenger = NewsMessenger()
        
    def run(self):
        print("🚀 [System] News Agent Version 2.0 (Marketing Leader Persona) Loaded")
        
        # 1. 수집 (주제 확장)
        queries = [
            "식품 산업 트렌드", "식음료 마케팅", "푸드테크", "MZ세대 식문화", 
            "편의점 신상", "대체육 시장", "건강기능식품 트렌드", "유통 업계 동향",
            "진주햄", "육가공 트렌드" # 자사/경쟁사 모니터링은 기본 포함
        ]
        raw_news = self.collector.collect(queries)
        
        # 2. 분석
        analyzed_news = self.strategist.analyze(raw_news)
        
        # 3. 보도
        report_path = self.editor.create_report(analyzed_news)
        
        # 4. 전송 (로컬 테스트 시 환경변수 없으면 스킵됨)
        self.messenger.send_report(report_path)
        
        return report_path

if __name__ == "__main__":
    system = NewsAgentSystem()
    system.run()