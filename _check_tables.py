import urllib.request
from bs4 import BeautifulSoup, Comment

url = "https://www.basketball-reference.com/players/m/malonka01/gamelog/1999"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req).read()
soup = BeautifulSoup(html, "html.parser")

# Check non-commented tables too
for t in soup.find_all("table"):
    print("DIRECT TABLE ID:", t.get("id"))

# Check inside HTML comments
for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
    if "table" in c.lower():
        frag = BeautifulSoup(c, "html.parser")
        for t in frag.find_all("table"):
            print("COMMENT TABLE ID:", t.get("id"))
