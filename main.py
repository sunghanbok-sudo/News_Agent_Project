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
                
            soup = BeautifulSoup(res.text, 'xml') # XML 파싱
            xml_items = soup.select("item")
            
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
    """
    def __init__(self):
        self.target_keywords = ["진주햄", "육가공", "HMR", "K-Food", "바질 후랑크", "남해마늘햄", "천하장사"]
        self.trend_keywords = ["비건", "단백질", "캠핑", "가성비", "편의점", "MZ세대"]
        self.critical_keywords = ["ASF", "아프리카돼지열병", "구제역", "원자재 가격"] # 위기 요인

    def analyze(self, news_list):
        print("📊 [전략 분석가] 뉴스 분석 및 점수 산정 중...")
        analyzed_list = []
        
        for news in news_list:
            score = 0
            reasons = []
            
            # 1. 핵심 키워드 매칭
            for kw in self.target_keywords:
                if kw in news['title'] or kw in news['desc']:
                    score += 10
                    reasons.append(f"핵심({kw})")
            
            # 2. 트렌드 키워드 매칭
            for tk in self.trend_keywords:
                if tk in news['title'] or tk in news['desc']:
                    score += 5
                    reasons.append(f"트렌드({tk})")
            
            # 3. 위기 요소 모니터링 (가중치 높음)
            for ck in self.critical_keywords:
                if ck in news['title'] or ck in news['desc']:
                    score += 20
                    reasons.append(f"🚨위기감지({ck})")
                    news['is_critical'] = True

            news['score'] = score
            news['reasons'] = ", ".join(reasons)
            
            # 인사이트 초안 생성 (가상) - 실제 LLM 연동 시 여기가 핵심
            if score >= 15:
                news['insight'] = "진주햄의 신제품 마케팅과 연계 가능성 높음. 즉시 대응 필요."
            elif 'is_critical' in news:
                news['insight'] = "원료 수급 불안정 예상. 구매팀과 재고 파악 필요."
            else:
                news['insight'] = "시장 동향 파악용 참고 자료."
                
            analyzed_list.append(news)
            
        # 점수 내림차순 정렬
        analyzed_list.sort(key=lambda x: x['score'], reverse=True)
        return analyzed_list


class NewsEditor:
    """
    [에이전트 3: 편집장]
    - 리포트 포맷팅 (가독성 최우선)
    - Markdown 파일 생성
    """
    def create_report(self, analyzed_news):
        print("📝 [편집장] 데일리 인사이트 리포트 작성 중...")
        
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
            
        today_str = datetime.now().strftime("%Y-%m-%d")
        file_path = os.path.join(OUTPUT_DIR, f"Daily_Insight_Report_{today_str}.md")
        
        # 상위 5개 + 위기 뉴스 필터링
        top_news = analyzed_news[:5]
        
        markdown_content = f"# 📰 진주햄 마케팅 데일리 인사이트 ({today_str})\n\n"
        markdown_content += "> **요약**: 오늘의 주요 시장 동향과 마케팅 기회를 정리해 드립니다.\n\n"
        
        for i, news in enumerate(top_news):
            icon = "🚨" if news.get('is_critical') else "💡"
            markdown_content += f"## {i+1}. {icon} {news['title']}\n"
            markdown_content += f"- **관련 키워드**: {news['reasons']}\n"
            markdown_content += f"- **핵심 요약**: {news['desc']}\n"
            markdown_content += f"- **마케팅 인사이트**: {news['insight']}\n"
            markdown_content += f"- **원문**: [링크 바로가기]({news['link']})\n\n"
            markdown_content += "---\n\n"
            
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
            msg['Subject'] = f"📢 [News] 진주햄 데일리 리포트 ({datetime.now().strftime('%Y-%m-%d')})"
            
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
        # 1. 수집
        queries = ["육가공 트렌드", "진주햄", "아프리카돼지열병", "편의점 안주"]
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