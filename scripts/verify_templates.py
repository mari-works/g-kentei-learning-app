#!/usr/bin/env python3
"""
検証用スクリプト: テンプレートが正しく配置されているか確認
"""

from pathlib import Path

TEMPLATES = [
    'base.html',
    'home.html',
    'big_categories.html',
    'categories.html',
    'keywords.html',
    'keyword_detail.html',
    'practice.html',
    'history.html',
]

template_dir = Path(__file__).resolve().parent / 'templates'

print("📋 テンプレートファイル確認")
print("=" * 50)

for tmpl in TEMPLATES:
    path = template_dir / tmpl
    exists = "✓" if path.exists() else "✗"
    size = f"({path.stat().st_size} bytes)" if path.exists() else "(未作成)"
    print(f"{exists} {tmpl:30} {size}")

print("=" * 50)
print("\n🎨 デザイン特性:")
print("  • 左固定サイドバー (lg:w-72)")
print("  • メインコンテンツエリア")
print("  • 上部検索バー")
print("  • カード/リスト型レイアウト")
print("  • 余白は lg:px-8, py-6")
print("  • 角丸 rounded-[28px] ~ [32px]")
print("  • アクセント: sky-600")
print("  • 背景: slate-100")
