# TrayTranslate

A simple tool that leverages the OpenAI API to translate stuff on the screen when a shortcut is being pressed

The shortcut in this case is hardcoded as **Shift+Ctrl+PrtScr**

To use this app, you'd first need to populate `.env` with your OpenAI API key. Refer to `.env.example` to get a reference.

After that, enable the venv:

```
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

And then you can either run the app directly using: `python .\tray_translate_picker.py`

Or build into a standalone .exe with: `pyinstaller --onefile --noconsole  .\tray_translate_picker.py`