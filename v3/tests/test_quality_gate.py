import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_main_module():
    sys.modules.setdefault("requests", types.SimpleNamespace(get=lambda *a, **k: None))

    class DummySoup:
        def __init__(self, *args, **kwargs):
            self.text = ""
        def select(self, *args, **kwargs):
            return []
        def select_one(self, *args, **kwargs):
            return None
        def find_all(self, *args, **kwargs):
            return []

    sys.modules.setdefault("bs4", types.SimpleNamespace(BeautifulSoup=DummySoup))
    google_mod = types.ModuleType("google")
    genai_mod = types.ModuleType("google.genai")
    genai_mod.Client = lambda *args, **kwargs: None
    genai_mod.types = types.SimpleNamespace(GenerateContentConfig=lambda *args, **kwargs: None)
    google_mod.genai = genai_mod
    sys.modules.setdefault("google", google_mod)
    sys.modules.setdefault("google.genai", genai_mod)
    sys.modules.setdefault("google.genai.types", genai_mod.types)
    sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))

    spec = importlib.util.spec_from_file_location("news_v3_main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llm_failure_fallback_filters_non_food_and_classifies_food_articles(monkeypatch):
    module = load_main_module()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    strategist = module.NewsStrategist()
    news_list = [
        {"title": "‘도깨비’ 한 주 결방했던 진짜 이유…공유, 10년 만에 다 털어놨다", "desc": "방송 드라마 여행 예능", "link": "https://example.com/drama"},
        {"title": "맛은 기본, 재미는 덤…식품·외식업계, 체험형 콘텐츠로 영토 확장", "desc": "외식 맛집 방문 체험형 콘텐츠", "link": "https://example.com/experience"},
        {"title": "CJ제일제당, HMR 신제품 출시로 편의점 유통 확대", "desc": "식품 HMR 간편식 신제품 출시 매출 유통", "link": "https://example.com/food"},
        {"title": "원재료 가격 상승에 식품업계 원가 부담 확대", "desc": "축산 원자재 환율 가격 인상", "link": "https://example.com/cost"},
    ]

    result = strategist.analyze(news_list)
    titles = [item["title"] for item in result]

    assert "‘도깨비’ 한 주 결방했던 진짜 이유…공유, 10년 만에 다 털어놨다" not in titles
    assert "맛은 기본, 재미는 덤…식품·외식업계, 체험형 콘텐츠로 영토 확장" not in titles
    assert "CJ제일제당, HMR 신제품 출시로 편의점 유통 확대" in titles
    assert "원재료 가격 상승에 식품업계 원가 부담 확대" in titles
    assert all(item["category"] != "미분류" for item in result)
    assert all(item["score"] >= 60 for item in result)
