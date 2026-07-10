import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import json
import os
import sys
import email.utils
import difflib
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- 설정 및 상수 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data", "raw_news")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs", "reports")

CATEGORY_ORDER = [
    "국제 이슈",
    "유통/시장 시황",
    "물가 및 원재료",
    "트렌드 및 신기술/신제품",
    "국내 식품 핫뉴스",
]

CATEGORY_TARGETS = {
    "국제 이슈": 2,
    "유통/시장 시황": 6,
    "물가 및 원재료": 2,
    "트렌드 및 신기술/신제품": 3,
    "국내 식품 핫뉴스": 2,
}

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
                
                # [추가] 광고/홍보성 기사 패턴 필터링 (단일 채널 + 제품 출시/할인)
                if not is_excluded:
                    ad_patterns = [
                        r".*출시.*", r".*선보여.*", r".*할인.*", r".*이벤트.*", r".*기획전.*",
                        r".*팝업.*", r".*콜라보.*"
                    ]
                    # 채널명이 포함된 경우 (예: 세븐일레븐, 이마트 등)
                    channel_patterns = [r"세븐일레븐", r"GS25", r"CU", r"이마트", r"홈플러스", r"롯데마트"]
                    
                    has_ad_keyword = any(re.search(p, title) for p in ad_patterns)
                    has_channel = any(re.search(p, title) for p in channel_patterns)
                    
                    if has_ad_keyword and has_channel:
                        print(f"🚫 [수집가] 광고/홍보성 패턴 감지하여 수집 제외: {title}")
                        is_excluded = True
                        
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
        # Gemini API Key 확인
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            print("⚠️ [전략 분석가] GEMINI_API_KEY가 설정되지 않았습니다. .env 파일이나 환경 변수를 확인해 주세요.")

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

    FOOD_RELEVANCE_KEYWORDS = [
        "식품", "식음료", "외식", "먹거리", "푸드", "k-푸드", "가공식품", "식자재",
        "진주햄", "천하장사", "식재료", "원재료", "농산물", "축산", "수입육", "육가공", "어육", "소시지",
        "소세지", "햄", "비엔나", "후랑크", "베이컨", "HMR", "간편식", "밀키트",
        "RMR", "냉동", "냉장", "라면", "김치", "스낵", "간식", "음료", "주류", "전통주", "맥주", "소주",
        "안주", "홈술", "제로슈거", "제로칼로리", "비건", "대체육", "단백질",
        "푸드테크", "헬시플레저", "할랄", "식중독", "이물질", "리콜", "위생",
        "식약처", "농식품부", "건강기능식품"
    ]

    BROAD_CONTEXT_KEYWORDS = {
        "마케팅", "캠페인", "콜라보", "팝업", "신제품", "매출", "ESG", "M&A",
        "숏폼", "유튜브", "가성비", "MZ", "잘파", "1인가구", "시니어", "오피스",
        "편의점", "할인점", "대형마트", "이커머스"
    }

    OFF_TOPIC_KEYWORDS = [
        "드라마", "영화", "예능", "배우", "가수", "아이돌", "팬미팅", "웹툰",
        "애니", "게임", "축구", "야구", "부동산", "아파트", "코인", "화장품",
        "패션", "여행", "항공", "호텔", "리조트", "관광", "자동차", "반도체",
        "스마트폰", "정치", "선거"
    ]

    CATEGORY_RULES = {
        "국제 이슈": ["해외", "글로벌", "수출", "동남아", "미국", "중국", "일본", "유럽", "할랄", "k-푸드", "진출", "국제"],
        "유통/시장 시황": ["유통", "편의점", "마트", "대형마트", "할인점", "이커머스", "온라인", "커머스", "PB", "B2B", "채널", "시장", "슈퍼"],
        "물가 및 원재료": ["물가", "원재료", "원자재", "가격", "단가", "인상", "인플레이션", "환율", "수급", "수입육", "애그플레이션"],
        "트렌드 및 신기술/신제품": ["트렌드", "푸드테크", "신기술", "신제품", "헬시플레저", "제로", "비건", "대체육", "단백질", "밀키트", "RMR"],
        "국내 식품 핫뉴스": ["식품업계", "외식업계", "정책", "실적", "팝업", "콜라보", "리뉴얼", "출시", "협업", "식약처", "농식품부"],
    }

    def _article_text(self, news, include_query=False):
        parts = [
            str(news.get("title", "")),
            str(news.get("desc", "")),
        ]
        if include_query:
            parts.append(str(news.get("query", "")))
        return " ".join(parts).lower()

    def _keyword_hits(self, keywords, text):
        hits = []
        for keyword in keywords:
            keyword_text = str(keyword or "").strip()
            if keyword_text and keyword_text.lower() in text:
                hits.append(keyword_text)
        return hits

    def _food_context_keywords(self):
        configured_keywords = (
            self.biz_keywords
            + self.trend_keywords
            + self.risk_keywords
            + self.competitor_keywords
        )
        return [
            keyword for keyword in configured_keywords
            if keyword and keyword not in self.BROAD_CONTEXT_KEYWORDS
        ]

    def _is_food_relevant(self, news):
        text = self._article_text(news)
        if not text.strip():
            return False

        direct_hits = self._keyword_hits(self.FOOD_RELEVANCE_KEYWORDS, text)
        configured_hits = self._keyword_hits(self._food_context_keywords(), text)
        off_topic_hits = self._keyword_hits(self.OFF_TOPIC_KEYWORDS, text)

        if off_topic_hits and not direct_hits and not configured_hits:
            return False

        return bool(direct_hits or configured_hits)

    def _classify_rule_based(self, news):
        text = self._article_text(news, include_query=True)

        # 원재료/물가 이슈는 보고 우선도가 높으므로 먼저 잡습니다.
        priority_order = [
            "물가 및 원재료",
            "국제 이슈",
            "유통/시장 시황",
            "트렌드 및 신기술/신제품",
            "국내 식품 핫뉴스",
        ]

        best_category = "국내 식품 핫뉴스"
        best_hits = []
        for category in priority_order:
            hits = self._keyword_hits(self.CATEGORY_RULES[category], text)
            if hits:
                best_category = category
                best_hits = hits
                break

        if not best_hits:
            best_hits = self._keyword_hits(self.FOOD_RELEVANCE_KEYWORDS, text)

        return best_category, best_hits[:3]

    def _is_lightweight_promo(self, news):
        text = self._article_text(news)
        promo_hits = self._keyword_hits(
            ["출시", "선보여", "할인", "이벤트", "기획전", "팝업", "콜라보", "판매"],
            text,
        )
        strategic_hits = self._keyword_hits(
            ["시장", "성장", "확산", "수출", "규제", "물가", "원재료", "업계", "전략", "실적", "인상"],
            text,
        )
        return bool(promo_hits and not strategic_hits)

    def _score_rule_based(self, news, category):
        text = self._article_text(news, include_query=True)
        score = 50

        if self._keyword_hits(self.competitor_keywords + ["진주햄", "천하장사"], text):
            score += 18
        if category == "물가 및 원재료":
            score += 14
        if category == "유통/시장 시황":
            score += 10
        if category == "국제 이슈":
            score += 9
        if category == "트렌드 및 신기술/신제품":
            score += 8
        if self._keyword_hits(["육가공", "소시지", "햄", "HMR", "간편식", "수입육", "단백질"], text):
            score += 10
        if self._is_lightweight_promo(news):
            score -= 12

        return max(1, min(score, 100))

    def _fallback_insight(self, category):
        insights = {
            "국제 이슈": "국내 식품 제조사의 수출, 원가, 브랜드 포지셔닝에 영향을 줄 수 있는 글로벌 흐름입니다.",
            "유통/시장 시황": "채널별 수요 변화와 판매 전략을 점검할 때 참고할 만한 시장 신호입니다.",
            "물가 및 원재료": "원가와 판가, 프로모션 강도에 직접 영향을 줄 수 있어 추적이 필요합니다.",
            "트렌드 및 신기술/신제품": "제품 기획과 커뮤니케이션 소재를 점검할 때 참고할 수 있는 소비 트렌드입니다.",
            "국내 식품 핫뉴스": "국내 식품업계의 경쟁 구도와 마케팅 방향을 읽는 데 필요한 기사입니다.",
        }
        return insights.get(category, insights["국내 식품 핫뉴스"])

    def _curate_rule_based(self, news_list, fallback_reason):
        print(f"🧭 [전략 분석가] {fallback_reason}: 룰 기반 식품 관련성 필터와 카테고리 분류를 적용합니다.")
        enriched_news = []
        seen_keys = set()

        for news in news_list:
            if not self._is_food_relevant(news):
                print(f"🚫 [전략 분석가] 식품 관련성 부족으로 제외: {news.get('title', '')}")
                continue

            category, reason_hits = self._classify_rule_based(news)
            curated = dict(news)
            curated["category"] = category
            curated["score"] = self._score_rule_based(curated, category)
            curated["reasons"] = ", ".join(reason_hits) if reason_hits else "식품 관련 키워드"
            curated["insight"] = self._fallback_insight(category)
            curated["is_critical"] = bool(
                self._keyword_hits(["식중독", "이물질", "리콜", "회수", "급등", "파동"], self._article_text(curated))
            )

            dedupe_key = curated.get("link") or re.sub(r"\W+", "", curated.get("title", "").lower())
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            enriched_news.append(curated)

        by_category = {category: [] for category in CATEGORY_ORDER}
        for news in enriched_news:
            by_category.setdefault(news["category"], []).append(news)
        for items in by_category.values():
            items.sort(key=lambda item: item.get("score", 0), reverse=True)

        selected = []
        selected_keys = set()
        for category in CATEGORY_ORDER:
            target = CATEGORY_TARGETS.get(category, 0)
            for news in by_category.get(category, [])[:target]:
                key = news.get("link") or news.get("title")
                selected.append(news)
                selected_keys.add(key)

        remaining = sorted(enriched_news, key=lambda item: item.get("score", 0), reverse=True)
        for news in remaining:
            if len(selected) >= 15:
                break
            key = news.get("link") or news.get("title")
            if key in selected_keys:
                continue
            selected.append(news)
            selected_keys.add(key)

        if len(selected) < 15:
            print(f"ℹ️ [전략 분석가] 식품 관련 기사만 선별하여 {len(selected)}건을 반환합니다. 품질 유지를 위해 15건을 억지로 채우지 않습니다.")

        return selected[:15]

    def analyze(self, news_list):
        print("📊 [전략 분석가] 뉴스 분석 및 Top 15 선정 중 (Gemini 카테고리별 할당)...")
        
        if not news_list:
            print("⚠️ [전략 분석가] 수집된 뉴스가 없어 빈 리스트를 반환합니다.")
            return []

        candidate_news_list = []
        for news in news_list:
            if self._is_food_relevant(news):
                candidate_news_list.append(news)
            else:
                print(f"🚫 [전략 분석가] LLM 분석 전 식품 관련성 부족으로 제외: {news.get('title', '')}")

        if not candidate_news_list:
            print("⚠️ [전략 분석가] 식품 관련 후보가 없어 빈 리스트를 반환합니다.")
            return []

        if not self.api_key:
            print("⚠️ [전략 분석가] GEMINI_API_KEY가 없어 룰 기반 선별로 대체합니다.")
            return self._curate_rule_based(candidate_news_list, "Gemini 연결 실패")
            
        # LLM에게 전달할 뉴스 데이터 축약 (전체 텍스트 대신 제목/설명만 제공하여 토큰 절약)
        # 최적화 적용(보보팀장): LLM 전송 기사 수 최대 60개 제한, desc 100자 절사, 불필요한 source 제거 (Gemini 3 기반)
        prompt_news_data = []
        optimized_news_list = candidate_news_list[:60]
        for idx, news in enumerate(optimized_news_list):
            desc_text = news.get('desc', '')
            if len(desc_text) > 100:
                desc_text = desc_text[:98] + ".."
            prompt_news_data.append({
                "id": idx,
                "title": news['title'],
                "desc": desc_text
            })

        system_prompt = f"""
당신은 대한민국 최고의 식품/유통 산업 전문 '마케팅 전략 분석가'입니다. 
주 타겟 독자는 50대 식품 제조사(특히 육가공/HMR 주력) 마케팅 팀장입니다.

다음은 오늘 수집 후 1차 관련성 검증을 통과한 뉴스 기사 목록({len(candidate_news_list)}건)입니다.
이 중에서 마케팅 팀장님께서 반드시 알아야 할 **가장 중요하고 인사이트가 넘치는 기사를 최대 15개** 엄선해 주세요.
식품 제조, 식품 유통, 외식, 원재료, 식품 소비 트렌드와 직접 관련성이 낮은 기사는 제외하고, 관련 기사가 부족하면 15개 미만으로 반환해도 됩니다. 절대 비식품 뉴스를 개수 채우기용으로 포함하지 마세요.

## 필수 선정 카테고리 및 목표 배분 (최대 15개)
다음 5개 카테고리별 목표 개수에 맞게 기사를 배분하여 선정하세요.
단, 특정 카테고리에 해당하는 기사가 부족할 경우, 관련성이 높은 다른 카테고리에서만 보강하고 비식품 기사로 개수를 채우지 마세요.

1. **국제 이슈 (목표 2개)**: 국내 식품업계에 영향을 미치는 글로벌 K-푸드 수출, 해외 진출 동향, 국제 규제 등
2. **유통/시장 시황 (목표 6개)**: 할인점, 편의점, 개인 슈퍼, 온라인 커머스, B2B 시장 등 유통 채널 및 시장 상황 동향
3. **물가 및 원재료 (목표 2개)**: 인플레이션, 원자재 가격 변동, 애그플레이션, 식재료 수급 관련 이슈 (위기 요인 포함)
4. **트렌드 및 신기술/신제품 (목표 3개)**: 푸드테크, 헬시플레저, 비건, 주요 경쟁사의 혁신적인 신제품 및 신기술
5. **국내 식품 핫뉴스 (목표 2개)**: 그 외 국내 식품업계 전반의 주요 정책, 팝업스토어, 영업 실적, 콜라보레이션 등 핫이슈

## 선정에서 제외할 기사 (중요)
- **단순 광고/홍보성 기사**: 특정 유통 채널(편의점, 대형마트 등)에서 단순히 신제품을 출시했다거나 할인 행사를 한다는 기사는 지침이 없는 한 제외하십시오. (예: "XX편의점, YY치킨 출시", "ZZ마트, AA할인전")
- **단순 보도자료**: 인사이트 없이 특정 기업의 단순 동정이나 일반적인 제품 홍보 기사는 지양합니다.
- **이미 다룬 내용**: 중복된 주제나 이미 널리 알려진 신제품 뉴스는 점수를 낮게 부여하십시오.

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
    ... (최대 15개)
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
            client = genai.Client(api_key=self.api_key)
            prompt_input = f"{system_prompt}\n\n뉴스 데이터:\n{json.dumps(prompt_news_data, ensure_ascii=False)}"
            
            response = client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=prompt_input,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    temperature=0.1 # 분류 정확도 향상을 위해 온도를 낮춤
                )
            )
            content = response.text
            
            # 파싱 보정 (마크다운 코드 블록 제거)
            clean_content = content.strip()
            if clean_content.startswith("```json"):
                clean_content = clean_content[7:]
            elif clean_content.startswith("```"):
                clean_content = clean_content[3:]
            if clean_content.endswith("```"):
                clean_content = clean_content[:-3]
            clean_content = clean_content.strip()
            
            try:
                result_json = json.loads(clean_content)
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
                return self._curate_rule_based(candidate_news_list, "Gemini 응답 파싱 실패")

            top_results = result_json[:15]
            
            final_news_list = []
            for item in top_results:
                orig_id = item.get("original_id")
                # 안전 변환: 문자열 "3" → int 3
                try:
                    orig_id = int(orig_id) if orig_id is not None else None
                except (ValueError, TypeError):
                    print(f"⚠️ [전략 분석가] original_id 변환 실패: {orig_id}")
                    continue
                    
                if orig_id is not None and 0 <= orig_id < len(candidate_news_list):
                    news = dict(candidate_news_list[orig_id])
                    if not self._is_food_relevant(news):
                        print(f"🚫 [전략 분석가] LLM 선정 후 검증에서 식품 관련성 부족으로 제외: {news.get('title', '')}")
                        continue

                    fallback_category, fallback_hits = self._classify_rule_based(news)
                    category = str(item.get("category", "")).strip()
                    if category not in CATEGORY_ORDER:
                        category = fallback_category

                    news['score'] = item.get("score", 0)
                    news['reasons'] = item.get("reasons", "") or ", ".join(fallback_hits) or "식품 관련 키워드"
                    news['insight'] = item.get("insight", "") or self._fallback_insight(category)
                    news['is_critical'] = item.get("is_critical", False)
                    news['category'] = category
                    final_news_list.append(news)
                else:
                    print(f"⚠️ [전략 분석가] original_id 범위 초과: {orig_id} (총 {len(candidate_news_list)}건)")
            
            # 매핑 결과가 너무 적으면 (예: LLM이 이상한 응답) 폴백
            if len(final_news_list) < 5:
                print(f"⚠️ [전략 분석가] 매핑 결과가 {len(final_news_list)}건으로 너무 적어 폴백합니다.")
                return self._curate_rule_based(candidate_news_list, "Gemini 매핑 실패")

            if len(final_news_list) < 15:
                supplemental_news = self._curate_rule_based(candidate_news_list, "Gemini 결과 보강")
                seen_keys = {news.get("link") or news.get("title") for news in final_news_list}
                for news in supplemental_news:
                    if len(final_news_list) >= 15:
                        break
                    key = news.get("link") or news.get("title")
                    if key in seen_keys:
                        continue
                    final_news_list.append(news)
                    seen_keys.add(key)
                    
            print(f"✅ [전략 분석가] Gemini 분석 완료: {len(final_news_list)}건 선정.")
            return final_news_list

        except Exception as e:
            print(f"❌ [전략 분석가] Gemini API 호출 실패 (상세에러): {str(e)}")
            return self._curate_rule_based(candidate_news_list, "Gemini API 호출 실패")



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
        
        markdown_content = f"# 📊 식품 관련 뉴스 클리핑\n\n"
        markdown_content += f"> **{today_str}** | 진주햄 가족을 위한 금주의 뉴스 요약\n\n"
        
        # 카테고리별 그룹화 (고정 순서 적용)
        CATEGORY_ORDER = [
            "국제 이슈",
            "유통/시장 시황",
            "물가 및 원재료",
            "트렌드 및 신기술/신제품",
            "국내 식품 핫뉴스",
            "미분류"
        ]
        
        categorized_news = {cat: [] for cat in CATEGORY_ORDER}
        for news in top_news:
            cat = news.get('category', '미분류').strip()
            # 정확히 일치하지 않으면 '미분류'로 강제 편입 (기사 누락 방지)
            if cat not in CATEGORY_ORDER:
                cat = '미분류'
            categorized_news[cat].append(news)
            
        for category in CATEGORY_ORDER:
            items = categorized_news.get(category, [])
            if not items:
                continue
            markdown_content += f"## ◼ {category}\n\n"
            for news in items:
                icon = "🔹"
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
            # 카테고리별 그룹화 (고정 순서 적용)
            CATEGORY_ORDER = [
                "국제 이슈",
                "유통/시장 시황",
                "물가 및 원재료",
                "트렌드 및 신기술/신제품",
                "국내 식품 핫뉴스",
                "미분류"
            ]

            categorized_news = {cat: [] for cat in CATEGORY_ORDER}
            for news in report_data:
                cat = news.get('category', '미분류').strip()
                if cat not in CATEGORY_ORDER:
                    cat = '미분류'
                categorized_news[cat].append(news)

            # 카테고리별 색상 맵 (고급스러우면서도 초여름에 걸맞은 산뜻한 HSL 톤 적용)
            CATEGORY_COLORS = {
                "국제 이슈": "#1E3D59",           # 세련된 클래식 마린 네이비
                "유통/시장 시황": "#17B890",       # 청량한 초여름 세이지 민트 그린
                "물가 및 원재료": "#E05A47",       # 화사한 소프트 코랄 레드
                "트렌드 및 신기술/신제품": "#8E44AD", # 트렌디한 라벤더 바이올렛
                "국내 식품 핫뉴스": "#F39C12",     # 생기 있는 써머 오렌지
                "미분류": "#7F8C8D",               # 차분한 슬레이트 그레이
            }
            CATEGORY_ICONS = {
                "국제 이슈": "GLOBAL",
                "유통/시장 시황": "MARKET",
                "물가 및 원재료": "MATERIAL",
                "트렌드 및 신기술/신제품": "TREND",
                "국내 식품 핫뉴스": "HOT",
                "미분류": "ETC",
            }

            # 각 기사 행 생성 (신문 스타일)
            news_items_html = ""
            article_no = 0
            for category in CATEGORY_ORDER:
                items = categorized_news.get(category, [])
                if not items:
                    continue

                color = CATEGORY_COLORS.get(category, "#3A3A3A")
                label = CATEGORY_ICONS.get(category, "ETC")
                # 카테고리 섹션 헤더 (신문 구분선 스타일)
                news_items_html += f"""
                <tr><td colspan="2" style="padding: 20px 0 6px 0;">
                    <table width="100%" cellpadding="0" cellspacing="0" border="0">
                        <tr>
                            <td style="border-top: 3px solid #111; padding-top: 4px; vertical-align: top; width: 5px; padding-right: 10px;">
                                <div style="background-color: {color}; color: #fff; font-size: 9px; font-weight: 900; letter-spacing: 1px; padding: 2px 6px; white-space: nowrap; display: inline-block; border-radius: 2px;">{label}</div>
                            </td>
                            <td style="border-top: 1px solid #CCC; padding-top: 4px; vertical-align: top;">
                                <span style="color: {color}; font-size: 13px; font-weight: 900; letter-spacing: 0.5px; text-transform: uppercase;">{category}</span>
                                <span style="color: #999; font-size: 11px; margin-left: 8px;">— {len(items)}건</span>
                            </td>
                        </tr>
                    </table>
                </td></tr>
                """
                for news in items:
                    article_no += 1

                    # 날짜 처리
                    pub_date = news.get('pub_date', '')
                    try:
                        import email.utils as eu
                        dt = eu.parsedate_to_datetime(pub_date)
                        date_str = dt.strftime('%m.%d')
                    except:
                        date_str = ""

                    # 기사 주요 내용 요약 (desc, 2~3줄)
                    desc_text = news.get('desc', '')
                    if len(desc_text) > 230:
                        desc_text = desc_text[:228].rstrip() + "…"
                    # 제목과 중복되는 경우 제거 (RSS desc가 title과 동일한 경우 빈번함)
                    title_clean = re.sub(r'[^\w]', '', news['title'].lower())
                    desc_clean_check = re.sub(r'[^\w]', '', desc_text.lower())
                    if title_clean and desc_clean_check and (
                        desc_clean_check.startswith(title_clean[:30]) or
                        (len(title_clean) > 0 and len(desc_clean_check) > 0 and
                         len(set(title_clean) & set(desc_clean_check)) / max(len(set(title_clean)), 1) > 0.85 and
                         len(desc_clean_check) <= len(title_clean) * 1.3)
                    ):
                        desc_text = ""


                    date_html = f'<span style="color:#999;font-size:10px;margin-left:6px;">({date_str})</span>' if date_str else ""

                    item_html = f"""
                    <tr><td colspan="2" style="padding: 10px 0; border-bottom: 1px dotted #DDD;">
                        <table width="100%" cellpadding="0" cellspacing="0" border="0">
                            <tr>
                                <td style="vertical-align: top; width: 22px; padding-top: 1px; padding-right: 8px; color: {color}; font-size: 11px; font-weight: 900; font-family: 'Georgia', serif;">{article_no}</td>
                                <td style="vertical-align: top;">
                                    <div>
                                        <a href="{news['link']}" style="color: #111; text-decoration: none; font-size: 14px; font-weight: 700; line-height: 1.4; font-family: 'Georgia', 'Times New Roman', serif;">{news['title']}</a>{date_html}
                                    </div>
                                    <p style="margin: 6px 0 0 0; font-size: 12.5px; color: #444; line-height: 1.65;">{desc_text}</p>
                                </td>
                            </tr>
                        </table>
                    </td></tr>
                    """
                    news_items_html += item_html

            total_articles = len(report_data)
            today_formatted = datetime.now().strftime('%Y년 %m월 %d일')
            issue_no = datetime.now().isocalendar()[1]  # 주차

            # 헤더 이미지 로드 및 CID 구성
            from email.mime.image import MIMEImage
            header_image_cid = "jinju_header"
            has_header_img = False
            
            # 다중 경로 후보지 탐색하여 진주햄 헤더 파일 검색
            header_img_paths = [
                os.path.join(BASE_DIR, "jinju_header.png"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "jinju_header.png"),
                os.path.join(BASE_DIR, "data", "jinju_header.png")
            ]
            
            header_img_path = ""
            for p in header_img_paths:
                if os.path.exists(p):
                    header_img_path = p
                    has_header_img = True
                    break
            
            if has_header_img:
                # 아웃룩에서는 height: auto가 먹히지 않고 이미지가 세로로 늘어날 수 있으므로 명시적인 width를 부여
                brand_banner_html = f"""
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 24px; margin-bottom: 8px;">
                    <tr>
                        <td align="center">
                            <img src="cid:{header_image_cid}" alt="Jinju Ham Family" width="636" style="width: 100%; max-width: 636px; height: auto; display: block; border: 0; border-radius: 8px;" />
                        </td>
                    </tr>
                </table>
                """
            else:
                brand_banner_html = """
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 24px; margin-bottom: 12px; background-color: #FFFFFF; border-radius: 8px;">
                    <tr>
                        <td align="center" style="padding: 10px;">
                            <img src="https://lh3.googleusercontent.com/cAAK-T4xQJf-YdM7uJEsuYSdQsd9WzHXyWhQA93ayqdZqmC3ipH5xWcmq3UBG5UJaIhpHJ0QFYXfGFlOAMoiEOL4MBPl3O-AwhIs26sn1qQ3Nfo2Ux5hSw=s0" alt="Jinju Ham Official Logo" height="38" style="height: 38px; display: block; border: 0;" />
                        </td>
                    </tr>
                </table>
                """

            html_content = f"""
            <!DOCTYPE html>
            <html lang="ko">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>식품 뉴스 클리핑</title>
                <!--[if mso]>
                <style type="text/css">
                    table {{border-collapse: collapse;}}
                    td, th {{font-family: Arial, sans-serif;}}
                </style>
                <![endif]-->
            </head>
            <body style="margin: 0; padding: 0; background-color: #E8F0EC; font-family: 'Helvetica Neue', 'Malgun Gothic', Arial, sans-serif;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#E8F0EC" style="width: 100%; margin: 0; padding: 30px 0; background-color: #E8F0EC;">
                <tr>
                    <td align="center" style="padding: 20px 0;">
                        <!--[if (gte mso 9)|(IE)]>
                        <table width="700" align="center" cellpadding="0" cellspacing="0" border="0">
                            <tr>
                                <td align="center">
                        <![endif]-->
                        <table class="main-container" width="100%" max-width="700" cellpadding="0" cellspacing="0" border="0" bgcolor="#FFFFFF" style="max-width: 700px; width: 100%; margin: 0 auto; background-color: #FFFFFF; border: 1px solid #DFECE6;">
                            <tr>
                                <td style="padding: 0 32px;">
                                    {brand_banner_html}
                                    
                                    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top: 15px; border-top: 2px solid #111; border-bottom: 1px solid #111;">
                                        <tr>
                                            <td align="center" style="padding: 5px 0; font-size: 10px; font-weight: 700; letter-spacing: 3px; color: #555;">
                                                JINJU HAM &nbsp;&middot;&nbsp; FOOD INDUSTRY INTELLIGENCE &nbsp;&middot;&nbsp; INTERNAL USE ONLY
                                            </td>
                                        </tr>
                                    </table>

                                    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin: 14px 0 8px 0;">
                                        <tr>
                                            <td align="center">
                                                <h1 style="margin: 0; font-size: 38px; font-weight: 900; letter-spacing: -1px; color: #111; font-family: 'Georgia', 'Times New Roman', serif; line-height: 1;">식품 뉴스 클리핑</h1>
                                                <p style="margin: 4px 0 0 0; font-size: 12px; color: #666; font-family: 'Georgia', serif; font-style: italic;">Weekly Food Industry News Curation — The Most Important Stories</p>
                                            </td>
                                        </tr>
                                    </table>

                                    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top: 2px solid #111; border-bottom: 2px solid #111; margin-bottom: 20px;">
                                        <tr>
                                            <td align="left" style="padding: 6px 0; font-size: 11px; font-weight: 700; color: #333; letter-spacing: 0.5px; width: 33%;">
                                                VOL. {datetime.now().year} &nbsp;|&nbsp; ISSUE {issue_no}
                                            </td>
                                            <td align="center" style="padding: 6px 0; font-size: 11px; color: #333; font-weight: 700; width: 34%;">
                                                선별 기사 {total_articles}건
                                            </td>
                                            <td align="right" style="padding: 6px 0; font-size: 11px; font-weight: 700; color: #333; width: 33%;">
                                                {today_formatted}
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding: 0 32px 32px 32px;">
                                    <table width="100%" cellpadding="0" cellspacing="0" border="0">
                                        {news_items_html}
                                    </table>
                                </td>
                            </tr>
                            <tr>
                                <td style="padding: 0 32px 24px 32px;">
                                    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top: 3px double #111;">
                                        <tr>
                                            <td align="center" style="padding-top: 14px;">
                                                <p style="margin: 0 0 3px 0; font-size: 10px; color: #999; letter-spacing: 0.5px;">Automatically curated by the AI Strategic Management System &middot; Powered by Google Gemini</p>
                                                <p style="margin: 0; font-size: 10px; color: #999;">&copy; {datetime.now().year} Jinju Ham Co., Ltd. Marketing &amp; Sales Division &nbsp;&middot;&nbsp; Strictly Confidential</p>
                                            </td>
                                        </tr>
                                    </table>
                                </td>
                            </tr>
                        </table>
                        <!--[if (gte mso 9)|(IE)]>
                                </td>
                            </tr>
                        </table>
                        <![endif]-->
                    </td>
                </tr>
            </table>
            </body>
            </html>
            """

            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.email_user, self.email_password)
                
                for recipient in self.recipients:
                    try:
                        # 인라인 이미지가 정상 렌더링되도록 related 구조로 생성
                        msg = MIMEMultipart('related')
                        msg['From'] = self.email_user
                        msg['To'] = recipient
                        msg['Subject'] = f"[Strategic Insight] 식품업계 비즈니스 브리핑 ({datetime.now().strftime('%m/%d')})"
                        
                        # 대체 본문 컨테이너 추가
                        msg_alternative = MIMEMultipart('alternative')
                        msg.attach(msg_alternative)
                        msg_alternative.attach(MIMEText(html_content, 'html'))
                        
                        # CID 이미지 첨부 (헤더 이미지 파일이 있을 경우)
                        if has_header_img:
                            try:
                                with open(header_img_path, 'rb') as img_f:
                                    mime_img = MIMEImage(img_f.read())
                                    mime_img.add_header('Content-ID', f'<{header_image_cid}>')
                                    mime_img.add_header('Content-Disposition', 'inline', filename='jinju_header.png')
                                    msg.attach(mime_img)
                            except Exception as img_err:
                                print(f"⚠️ [메신저] 헤더 이미지 CID 첨부 에러: {img_err}")
                        
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
