from pathlib import Path

catalog = Path(__file__).resolve().parents[1] / "plugin-market" / "catalog.json"
text = catalog.read_text(encoding="utf-8")
pairs = [
    ("https://raw.githubusercontent.com/HandaQAQ-Code/Intro-Voice-Plugin-Market/main/plugins",
     "https://gitee.com/handa2500/intro-voice-plugin-market/raw/main/plugins"),
    ("https://github.com/HandaQAQ-Code/Intro-Voice-Plugin-Market/tree/main/plugins",
     "https://gitee.com/handa2500/intro-voice-plugin-market/tree/main/plugins"),
]
for old, new in pairs:
    count = text.count(old)
    text = text.replace(old, new)
    print(f"{count} x {old}")
catalog.write_text(text, encoding="utf-8")
print("done")
