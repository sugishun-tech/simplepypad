# SimplePyPad

Windows XP のメモ帳ぐらい単純な GUI エディタを目指した Python 製エディタです。コードハイライトは Pygments を使い、ファイル拡張子から lexer を選びます。未保存ファイルや拡張子で判別できない場合は、メニューから Pygments alias を指定できます。

## 必要なもの

- Python 3.9 以降を想定
- Tkinter
- Pygments

Tkinter は多くの Python 配布に同梱されています。Linux では `python3-tk` などの追加パッケージが必要なことがあります。もちろん環境ごとの差異という、ソフトウェア界の伝統芸能は健在です。

## インストールと起動

```bash
python -m pip install -r requirements.txt
python simplepypad.py
```

ファイルを指定して起動する場合:

```bash
python simplepypad.py path/to/file.py
```

Windows なら同梱の `start_simplepypad.bat` からも起動できます。

## 主な機能

- New / Open / Save / Save As
- Undo / Redo / Cut / Copy / Paste
- Find / Find Next / Replace All / Go To Line
- 拡張子による Pygments コードハイライト
- Pygments alias による手動シンタックス指定
- Pygments style の変更
- Word Wrap 切り替え
- フォントサイズ変更
- Python によるカスタマイズ

## ハイライト

拡張子から Pygments lexer を選びます。例:

- `.py` -> Python
- `.js` -> JavaScript
- `.html` -> HTML
- `.rs` -> Rust
- `.go` -> Go

任意言語と言っても、宇宙の全言語ではなく Pygments が理解できる言語です。人間も文脈なしでは言語を理解しないので、そこは仲良く諦めてください。

メニューから `Tools -> Set Syntax by Pygments Alias...` を選ぶと `python`, `javascript`, `html`, `rust` などを手動指定できます。`Tools -> Auto Detect Syntax` で拡張子判定に戻ります。

巨大ファイルでは操作性優先でハイライトをスキップします。既定値は 300,000 文字です。

## カスタマイズ言語

カスタマイズ言語は Python です。`Tools -> Open User Config` でユーザー設定ファイルを開けます。

配置場所:

- Windows: `%APPDATA%\SimplePyPad\init.py`
- macOS / Linux: `~/.config/simplepypad/init.py`

設定ファイルは通常の Python として実行されます。サンドボックスではありません。信頼できないコードを実行しないでください。コンピュータは命令の善悪を判断せず、ただ破滅まで忠実です。

## カスタマイズ例

```python
# init.py
api.set_font("Consolas", 12)
api.set_theme("friendly")
api.set_option("wrap", False)

import datetime

def insert_timestamp(api):
    api.insert(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

api.add_command("custom.insert_timestamp", insert_timestamp)
api.add_menu_item("Tools", "Insert Timestamp", "custom.insert_timestamp")
api.bind_key("<F5>", lambda api, event: (api.run_command("custom.insert_timestamp"), "break")[-1])
```

## カスタマイズ API

`init.py` では `api` と `editor` が使えます。どちらも同じものです。

よく使うもの:

```python
api.get_text()
api.set_text("text", dirty=True)
api.insert("text")
api.replace_selection("text")
api.get_selection()

api.set_option("wrap", True)
api.set_option("tab_width", 2)
api.set_font("Consolas", 12)
api.set_theme("monokai")
api.set_language("python")      # None で自動判定に戻す

api.add_command("custom.name", func)
api.run_command("custom.name")
api.add_menu_item("Tools", "Label", "custom.name")
api.bind_key("<F5>", func)

api.on("after_open", func)
api.on("before_save", func)
api.on("after_save", func)
api.on("text_changed", func)
api.on("after_highlight", func)

api.open_file("path/to/file.txt")
api.save_file()
api.status("message")
api.show_message("title", "message")
```

コールバックはだいたい `func(api)` または `func(api, event)` の形で書けます。

## 起動オプション

```bash
python simplepypad.py [file]
python simplepypad.py --config ./my_init.py
python simplepypad.py --no-user-config
```

`--config` は複数指定できます。

## 実装方針

- GUI は標準ライブラリの Tkinter
- ハイライトは Pygments
- カスタマイズは `exec()` による Python 設定ファイル
- 余計なプロジェクト構造を避け、ほぼ単一ファイル

つまり、小さくて壊れにくいものを狙っています。巨大 IDE の真似を始めると、だいたい人類はプラグイン地獄を発明します。
