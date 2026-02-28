import requests
from bs4 import BeautifulSoup
import sys

sys.stdout.reconfigure(encoding='utf-8')

def debug_scraper():
    query = "진주햄"
    url = f"https://search.naver.com/search.naver?where=news&query={query}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
    
    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.text, 'html.parser')
    
    # Check if we can find the known title
    target_text = "분홍소시지" 
    print(f"Searching for '{target_text}' in HTML...")
    
    if target_text in res.text:
        print("✅ Found target text in raw HTML.")
        # Find the element containing this text
        found = soup.find(string=lambda text: text and target_text in text)
        if found:
            parent = found.parent
            print(f"Parent Tag: {parent.name}")
            print(f"Parent Classes: {parent.get('class')}")
            
            # Go up to find a container
            curr = parent
            for i in range(10):
                if curr:
                    print(f"Level {i}: {curr.name} | Classes: {curr.get('class')} | ID: {curr.get('id')}")
                    # Look for list item or something with 'bx' class (common in Naver)
                    if curr.name == 'li' or (curr.get('class') and 'bx' in curr.get('class')):
                        print(f"✅ Found potential container at Level {i}")
                        print(f"Container Text snippet: {curr.get_text()[:100]}...")
                        # Try to identify title and link from this container
                        link_node = curr.select_one("a.news_tit")
                        if link_node:
                            print(f"  - Found .news_tit: {link_node.text}")
                        else:
                            print("  - .news_tit NOT found in this container.")
                            # Try finding any 'a' tag
                            links = curr.find_all('a')
                            for idx, l in enumerate(links[:3]):
                                print(f"    - Link {idx}: Text='{l.get_text().strip()}', Class={l.get('class')}")
                        break
                    curr = curr.parent
    else:
        print("❌ Target text NOT found in raw HTML. User-Agent might be blocked or content matches different query results.")

if __name__ == "__main__":
    debug_scraper()
