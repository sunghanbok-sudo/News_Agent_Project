import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import json
import os
import sys
import email.utils
import difflib
from dotenv import load_dotenv

import os
import sys
import email.utils
import difflib
import openai
from dotenv import load_dotenv


# --- 설정 및 상수 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "raw_news")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs", "reports")

# .env 파일 로드 (환경변수 설정)
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=env_path)

# Windows 콘솔 인코딩 문제 해결
sys.stdout.reconfigure(encoding='utf-8')

# --- 에이전트 클래스 정의 ---

class NewsCollector:
    """
    [에이전트 1: 수집가]
    - 뉴스 데이터 수집 및 중복 제거
    - Raw Data 저장
    """
    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0"}
        # 제외 키워드 로드
        keywords_path = os.path.join(BASE_DIR, "keywords.json")
        self.exclude_keywords = []
        if os.path.exists(keywords_path):
            try:
                with open(keywords_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.exclude_keywords = data.get("exclude_keywords", [])
            except:
                pass
    
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

        # 중복 제거 (URL 및 N-gram 기반 글자 교집합 유사도 기준)
        seen_links = set()
        unique_news = []
        
        # 전처리 함수: 특수기호 제거 및 소문자화, 공백 제거 후 2글자 단위(Bi-gram) 셋 반환
        import re
        def get_ngram_set(text, n=2):
            # 언론사명, 괄호 등 공통 쓸모없는 패턴 제거
            clean_text = re.sub(r'\[.*?\]|\(.*?\)|\<.*?\>', '', text)
            # 모든 특수기호 및 공백까지 완전히 제거 (글자만 남김)
            clean_text = re.sub(r'[^\w]', '', clean_text).lower()
            
            # n-gram 추출 (예: '롯데웰푸드' -> '롯데', '데웰', '웰푸', '푸드')
            ngrams = set()
            for i in range(len(clean_text) - n + 1):
                ngrams.add(clean_text[i:i+n])
            return ngrams
            
        for n in all_news:
            if n['link'] in seen_links:
                continue
                
            n_ngrams = get_ngram_set(n['title'])
            
            # 의미적 중복(N-gram Jaccard 유사도) 검사
            is_semantic_duplicate = False
            
            # 제목이 너무 짧아(예: 3글자 미만) N-gram이 안나오는 경우 원본 그대로 difflib 보완
            if len(n_ngrams) < 2:
                for existing_news in unique_news:
                    similarity = difflib.SequenceMatcher(None, n['title'], existing_news['title']).ratio()
                    if similarity >= 0.70:
                        print(f"🚫 [수집가] 의미적 중복 기사 제외 (Sequence 유사도 {similarity:.2f}):\n  - 원본: {existing_news['title']}\n  - 중복: {n['title']}")
                        is_semantic_duplicate = True
                        break
            else:
                for existing_news in unique_news:
                    ex_ngrams = get_ngram_set(existing_news['title'])
                    if not ex_ngrams:
                        continue
                    
                    # N-gram 기반 Jaccard 유사도 중 Containment 비율 계산
                    # 짧은 쪽 제목 기준으로 몇 %의 글자 조합이 일치하는지 확인
                    intersection = n_ngrams.intersection(ex_ngrams)
                    min_len = min(len(n_ngrams), len(ex_ngrams))
                    
                    overlap_ratio = len(intersection) / min_len if min_len > 0 else 0
                    
                    # 2-gram 글자 조합이 45% (0.45) 이상 일치하면 무조건 동일 보도자료로 간주!
                    # 조사가 다르거나 띄어쓰기가 달라도 글자 조합은 대부분 교집합에 들어갑니다.
                    if overlap_ratio >= 0.45:
                        print(f"🚫 [수집가] 의미적 중복 기사 제외 (N-gram 포괄도 {overlap_ratio:.2f}):\n  - 원본: {existing_news['title']}\n  - 중복: {n['title']}")
                        is_semantic_duplicate = True
                        break
            
            if not is_semantic_duplicate:
                seen_links.add(n['link'])
                unique_news.append(n)

        self._save_raw_data(unique_news)
        return unique_news

    def _collect_naver(self, query):
        """네이버 뉴스 검색 (크롤링)"""
        items = []
        # pd=1: 1주, pd=4: 1일, pd=2: 1개월
        # 여기서는 1주 이내 기사만 검색
        url = f"https://search.naver.com/search.naver?where=news&query={query}&pd=1"
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
                
                # 제외 키워드 필터링
                content_to_check = title + " " + desc
                is_excluded = False
                for ex_kw in self.exclude_keywords:
                    if ex_kw in content_to_check:
                        print(f"🚫 [수집가] 제외 키워드 '{ex_kw}' 발견하여 수집 제외: {title}")
                        is_excluded = True
                        break
                        
                if is_excluded:
                    continue
                
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
        # 구글 뉴스 RSS URL (한국어 설정, 1주 이내: when:7d)
        url = f"https://news.google.com/rss/search?q={query} when:7d&hl=ko&gl=KR&ceid=KR:ko"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                print(f"⚠️ [수집가] 구글 RSS 요청 실패: {res.status_code}")
                return []
                
            try:
                soup = BeautifulSoup(res.text, 'xml') # XML 파싱 시도 (lxml 필요)
            except Exception:
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
                
                # 제외 키워드 필터링
                content_to_check = title + " " + desc_clean
                is_excluded = False
                for ex_kw in self.exclude_keywords:
                    if ex_kw in content_to_check:
                        print(f"🚫 [수집가] 제외 키워드 '{ex_kw}' 발견하여 수집 제외 (구글): {title}")
                        is_excluded = True
                        break
                        
                if is_excluded:
                    continue
                
                if title:
                    # 날짜 필터링 (Python 레벨에서 2차 검증)
                    pub_date_str = item.pubDate.text if item.pubDate else ""
                    is_recent = True
                    
                    if pub_date_str:
                        try:
                            # RSS pubDate (RFC 822) 파싱
                            pub_dt = email.utils.parsedate_to_datetime(pub_date_str)
                            # offset-aware와 unaware 비교를 위해 둘 다 aware로 맞추거나 변환
                            now = datetime.now(pub_dt.tzinfo) 
                            
                            diff = now - pub_dt
                            if diff > timedelta(days=7):
                                print(f"🚫 [수집가] 7일 지난 기사 제외: {title} ({diff.days}일 전)")
                                is_recent = False
                        except Exception as e:
                            print(f"⚠️ [수집가] 날짜 파싱 실패 ({pub_date_str}): {e}")

                    if is_recent:
                        items.append({
                            "title": title,
                            "link": link,
                            "desc": desc_clean,
                            "source": "Google",
                            "collected_at": datetime.now().isoformat(),
                            "query": query,
                            "pub_date": pub_date_str 
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
    - 뉴스 가치 평가 및 점수 산정 (OpenAI LLM 활용)
    - 인사이트(기회/위기) 도출
    - 타겟: 50대 식품 마케팅 팀장
    """
    def __init__(self):
        # OpenAI API Key 확인
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            print("⚠️ [전략 분석가] OPENAI_API_KEY가 설정되지 않았습니다. .env 파일을 확인해 주세요.")
        else:
            openai.api_key = self.api_key

        # 키워드 (LLM 프롬프트에 컨텍스트로 제공할 용도)
        keywords = self._load_keywords()
        self.biz_keywords = keywords.get("biz_keywords", [])
        self.trend_keywords = keywords.get("trend_keywords", [])
        self.risk_keywords = keywords.get("risk_keywords", [])
        self.target_keywords = keywords.get("target_keywords", [])
        self.competitor_keywords = keywords.get("competitor_keywords", [])

    def _load_keywords(self):
        """keywords.json 파일에서 분석용 키워드를 로드합니다."""
        keywords_path = os.path.join(BASE_DIR, "keywords.json")
        if os.path.exists(keywords_path):
            try:
                with open(keywords_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ [전략 분석가] 키워드 로딩 중 오류 발생: {e}")
        
        return {
            "biz_keywords": ["마케팅", "캠페인", "콜라보", "신제품", "매출"],
            "trend_keywords": ["제로", "비건", "푸드테크", "친환경"],
            "risk_keywords": ["물가", "인플레이션", "불매"],
            "target_keywords": ["MZ", "1인가구", "시니어"],
            "competitor_keywords": ["CJ제일제당", "롯데웰푸드", "하림"]
        }

    def analyze(self, news_list):
        print("📊 [전략 분석가] 뉴스 분석 및 Top 15 선정 중 (OpenAI 카테고리별 할당)...")
        
        if not self.api_key or not news_list:
            print("⚠️ [전략 분석가] API 키가 없거나 뉴스 목록이 비어 기존 Rule-based 로직(또는 빈 리스트)으로 폴백합니다.")
            fallback_list = news_list[:15]
            for news in fallback_list:
                news['score'] = 50
                news['reasons'] = "OpenAI 연결 실패(Fallback)"
                news['insight'] = "현재 분석 엔진에 접근할 수 없어 단순 상위 15개 기사를 추출했습니다."
                news['is_critical'] = False
                news['category'] = "미분류"
            return fallback_list
            
        # LLM에게 전달할 뉴스 데이터 축약 (전체 텍스트 대신 제목/설명만 제공하여 토큰 절약)
        prompt_news_data = []
        for idx, news in enumerate(news_list):
            prompt_news_data.append({
                "id": idx,
                "title": news['title'],
                "desc": news['desc'],
                "source": news['source']
            })

        system_prompt = f"""
당신은 대한민국 최고의 식품/유통 산업 전문 '마케팅 전략 분석가'입니다. 
주 타겟 독자는 50대 식품 제조사(특히 육가공/HMR 주력) 마케팅 팀장입니다.

다음은 오늘 수집된 뉴스 기사 목록({len(news_list)}건)입니다.
이 중에서 마케팅 팀장님께서 반드시 알아야 할 **가장 중요하고 인사이트가 넘치는 기사를 총 15개** 엄선해 주세요.

## 필수 선정 카테고리 및 목표 할당량 (총 15개)
다음 5개 카테고리별 목표 개수에 맞게 기사를 배분하여 선정하세요.
단, 특정 카테고리에 해당하는 기사가 부족할 경우, 부족한 개수만큼 기사가 풍부한 다른 카테고리(예: 트렌드, 핫뉴스)에서 기사를 추가로 선정하여 **반드시 총 15개를 채워야 합니다**.

1. **국제 이슈 (목표 2개)**: 국내 식품업계에 영향을 미치는 글로벌 K-푸드 수출, 해외 진출 동향, 국제 규제 등
2. **유통/시장 시황 (목표 6개)**: 할인점, 편의점, 개인 슈퍼, 온라인 커머스, B2B 시장 등 유통 채널 및 시장 상황 동향
3. **물가 및 원재료 (목표 2개)**: 인플레이션, 원자재 가격 변동, 애그플레이션, 식재료 수급 관련 이슈 (위기 요인 포함)
4. **트렌드 및 신기술/신제품 (목표 3개)**: 푸드테크, 헬시플레저, 비건, 주요 경쟁사의 혁신적인 신제품 및 신기술
5. **국내 식품 핫뉴스 (목표 2개)**: 그 외 국내 식품업계 전반의 주요 정책, 팝업스토어, 영업 실적, 콜라보레이션 등 핫이슈

## 응답 포맷 (반드시 JSON 포맷으로만 응답할 것)
```json
{{
  "articles": [
    {{
      "original_id": 0,
      "category": "유통/시장 시황",
      "score": 95,
      "reasons": "편의점 신상 간식 트렌드 확산",
      "insight": "편의점 채널에 맞춘 소용량/프리미엄 HMR 제품군 개발 및 벤치마킹 필요",
      "is_critical": false
    }},
    ... (총 15개)
  ]
}}
```
- `original_id`: 원본 뉴스 목록에 부여된 id 숫자
- `category`: 위 5개 카테고리 명칭 중 하나를 정확히 기재
- `score`: 중요도 점수 (1~100)
- `reasons`: 선정한 핵심 주제나 이유 (핵심 키워드 2~3개 중심)
- `insight`: 마케팅 팀장 관점에서 이 기사가 왜 중요한지('Why This Matters')에 대한 1~2문장의 전략적 코멘트
- `is_critical`: 대형 식중독, 리콜, 치명적 원자재 급등 등 즉시 보고/대응이 필요한 경우 예외적으로 true, 보통 false
"""
        
        try:
            client = openai.OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(prompt_news_data, ensure_ascii=False)}
                ],
                temperature=0.3,
                max_tokens=2000,
                response_format={ "type": "json_object" } # 강제 JSON 응답 (단, 최상위를 dict로 래핑해야 안전할 수 있음. 여기서는 프롬프트 지시를 믿음)
            )
            
            # OpenAI API의 json_object 모드는 최상위가 dict여야 하므로 프롬프트와 호환되게 처리
            # 하지만 최신 gpt-4o-mini 등은 배열 반환도 잘 지원하거나, 혹은 dict 내 배열을 꺼냅니다.
            content = response.choices[0].message.content
            
            # 파싱 보정
            try:
                result_json = json.loads(content)
                if "articles" in result_json:
                    result_json = result_json["articles"]
                elif isinstance(result_json, dict):
                    # fallback list extraction
                    for val in result_json.values():
                        if isinstance(val, list):
                            result_json = val
                            break
            except Exception as parse_e:
                print(f"⚠️ JSON 파싱 에러: {parse_e}\nContent: {content}")
                return news_list[:15]

            top_results = result_json[:15]
            
            final_news_list = []
            for item in top_results:
                orig_id = item.get("original_id")
                if orig_id is not None and 0 <= orig_id < len(news_list):
                    news = news_list[orig_id]
                    news['score'] = item.get("score", 0)
                    news['reasons'] = item.get("reasons", "")
                    news['insight'] = item.get("insight", "")
                    news['is_critical'] = item.get("is_critical", False)
                    news['category'] = item.get("category", "미분류")
                    final_news_list.append(news)
                    
            print(f"✅ [전략 분석가] OpenAI 분석 완료: {len(final_news_list)}건 선정.")
            return final_news_list

        except Exception as e:
            print(f"❌ [전략 분석가] OpenAI API 호출 실패: {e}")
            # 폴백: 점수가 없으므로 단순히 앞의 15개만 리턴하고 기본 정보 입력
            for news in news_list[:15]:
                news['score'] = 50
                news['reasons'] = "추출 실패 폴백"
                news['insight'] = "LLM API 오류로 인한 자동 추출"
                news['is_critical'] = False
                news['category'] = "미분류"
            return news_list[:15]



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
        
        # 상위 15개 선정 (점수가 너무 낮은건 제외할 수도 있음)
        top_news = analyzed_news[:15]
        
        markdown_content = f"# ☕ [Marketing Brief] 식품업계 모닝 인사이트 ({today_str})\n\n"
        markdown_content += "> **Executive Summary**: 엄선한 최근 식품 산업 트렌드와 주요 이슈 15선입니다.\n\n"
        
        # 카테고리별 그룹화
        categorized_news = {}
        for news in top_news:
            cat = news.get('category', '미분류')
            if cat not in categorized_news:
                categorized_news[cat] = []
            categorized_news[cat].append(news)
            
        for category, items in categorized_news.items():
            markdown_content += f"## 📌 {category}\n\n"
            for news in items:
                icon = "🚨" if news.get('is_critical') else "💡"
                markdown_content += f"### {icon} [{news['title']}]({news['link']})\n"
                markdown_content += f"- **Why This Matters**: {news['insight']}\n"
                markdown_content += f"- **Key Keywords**: {news['reasons']}\n\n"
            markdown_content += "---\n"
            
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)
            
        print(f"✅ [편집장] 리포트 발행 완료: {file_path}")
        return file_path, top_news

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
        
        # 수신자 목록 로드
        self.recipients = self._load_recipients()

    def _load_recipients(self):
        """recipients.json 파일에서 수신자 목록을 로드합니다."""
        recipients_path = os.path.join(BASE_DIR, "recipients.json")
        default_recipient = os.environ.get("GMAIL_TO", self.email_user)
        
        if os.path.exists(recipients_path):
            try:
                with open(recipients_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        print(f"👥 [메신저] 수신자 명단 로드 완료: {len(data)}명")
                        return data
            except Exception as e:
                print(f"⚠️ [메신저] 수신자 명단 로딩 중 오류 발생: {e}")
        
        # Fallback: 환경변수 또는 발신자 자신
        print(f"ℹ️ [메신저] 기본 수신자(환경변수)를 사용합니다: {default_recipient}")
        return [default_recipient]

    def send_report(self, report_path, report_data):
        print("📮 [메신저] 리포트 이메일 발송 준비...")
        
        if not self.email_user or not self.email_password:
            print("⚠️ [메신저] 이메일 설정(GMAIL_USER, GMAIL_APP_PASSWORD)이 없습니다. 발송을 건너뜁니다.")
            return

        try:
            # 카테고리별 그룹화
            categorized_news = {}
            for news in report_data:
                cat = news.get('category', '미분류')
                if cat not in categorized_news:
                    categorized_news[cat] = []
                categorized_news[cat].append(news)
                
            # HTML 생성
            news_items_html = ""
            for category, items in categorized_news.items():
                news_items_html += f"""
                <div style="margin-top: 30px; padding: 10px; background-color: #f1f8e9; border-radius: 5px;">
                    <h2 style="margin: 0; color: #2e7d32; font-size: 20px;">📌 {category}</h2>
                </div>
                """
                for news in items:
                    icon = "🚨" if news.get('is_critical') else "💡"
                    item_html = f"""
                    <div style="margin-bottom: 25px; padding-bottom: 15px; border-bottom: 1px solid #eee;">
                        <h3 style="margin-bottom: 10px; font-size: 16px;">
                            {icon} <a href="{news['link']}" style="color: #1a73e8; text-decoration: none;">{news['title']}</a>
                        </h3>
                        <p style="margin: 5px 0; font-size: 14px;"><strong>🎯 Why This Matters:</strong> {news['insight']}</p>
                        <p style="margin: 5px 0; color: #666; font-size: 13px;"><strong>🏷️ Key Keywords:</strong> {news['reasons']}</p>
                    </div>
                    """
                    news_items_html += item_html

            html_content = f"""
            <html>
            <body style="font-family: 'Malgun Gothic', sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; color: #333;">
                <div style="background-color: #f8f9fa; padding: 20px; border-radius: 10px; margin-bottom: 20px;">
                    <h1 style="margin: 0; color: #d32f2f; font-size: 24px;">☕ 식품업계 모닝 인사이트</h1>
                    <p style="margin: 10px 0 0 0; color: #666;">📅 {datetime.now().strftime('%Y-%m-%d')} | 진주햄 가족을 위한 일간 뉴스 브리프</p>
                </div>
                
                <div style="background-color: #fff3e0; padding: 15px; border-left: 5px solid #ff9800; margin-bottom: 20px;">
                    <strong>📢 Executive Summary:</strong> 카테고리별로 엄선한 핵심 산업 트렌드 및 시황 {len(report_data)}선입니다.
                </div>

                {news_items_html}

                <div style="margin-top: 40px; padding-top: 20px; border-top: 2px solid #eee; font-size: 12px; color: #999; text-align: center;">
                    <p>본 메일은 인공지능 전략 매니저에 의해 수집 및 분석 발송되었습니다.</p>
                    <p>© 2026 Sunghun Bok. All rights reserved.</p>
                </div>
            </body>
            </html>
            """

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_user, self.email_password)
                
                for recipient in self.recipients:
                    try:
                        msg = MIMEMultipart()
                        msg['From'] = self.email_user
                        msg['To'] = recipient
                        msg['Subject'] = f"☕ [Insight] 식품업계 모닝 브리핑 ({datetime.now().strftime('%Y-%m-%d')})"
                        msg.attach(MIMEText(html_content, 'html'))
                        
                        server.send_message(msg)
                        print(f"✅ [메신저] 이메일 발송 완료: {recipient}")
                    except Exception as e:
                        print(f"❌ [메신저] {recipient} 발송 실패: {e}")
            
        except Exception as e:
            print(f"❌ [메신저] 이메일 시스템 오류: {e}")

# --- 메인 실행부 ---
class NewsAgentSystem:
    def __init__(self):
        self.collector = NewsCollector()
        self.strategist = NewsStrategist()
        self.editor = NewsEditor()
        self.messenger = NewsMessenger()
        
    def run(self):
        print("🚀 [System] News Agent Version 2.0 (Marketing Leader Persona) Loaded")
        
        # 1. 수집 (keywords.json에서 쿼리 로드)
        keywords_path = os.path.join(BASE_DIR, "keywords.json")
        queries = []
        if os.path.exists(keywords_path):
            try:
                with open(keywords_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    queries = data.get("search_queries", [])
            except Exception as e:
                print(f"⚠️ [시스템] 검색 쿼리 로딩 중 오류 발생: {e}")
        
        if not queries:
            print("ℹ️ [시스템] 기본 검색 쿼리를 사용합니다.")
            queries = ["식품 산업 트렌드", "식음료 마케팅", "푸드테크", "진주햄"]
            
        raw_news = self.collector.collect(queries)
        
        # 2. 분석
        analyzed_news = self.strategist.analyze(raw_news)
        
        # 3. 보도 (HTML 데이터를 위해 top_news도 함께 반환받음)
        report_path, top_news = self.editor.create_report(analyzed_news)
        
        # 4. 전송 (데이터를 함께 전달하여 HTML 이메일 생성)
        self.messenger.send_report(report_path, top_news)
        
        return report_path

if __name__ == "__main__":
    system = NewsAgentSystem()
    system.run()