# Building a standalone Jarvis.exe (optional)

Running from the virtual environment (`scripts\run_jarvis.bat` or the
startup task) is the supported setup. An .exe is handy for a machine
without Python, but it's large (~400 MB+) and needs a few workarounds.

## Build

```powershell
cd jarvis
.venv\Scripts\activate
pip install pyinstaller
pyinstaller main.py --name Jarvis --noconsole --onedir ^
  --collect-data faster_whisper ^
  --collect-data openwakeword ^
  --collect-all ctranslate2 ^
  --collect-all onnxruntime ^
  --collect-submodules tools --collect-submodules core --collect-submodules ui ^
  --hidden-import pystray._win32 ^
  --hidden-import pycaw.pycaw --hidden-import comtypes.stream ^
  --hidden-import winotify ^
  --hidden-import keyboard --hidden-import pynput.keyboard._win32 ^
  --hidden-import pyautogui ^
  --add-data "config.yaml;." --add-data "README.md;."
copy .env dist\Jarvis\
```

Run `dist\Jarvis\Jarvis.exe --check` from a terminal first.
(`--noconsole` hides output, so for debugging build once without it.)

## Known pitfalls

- **Use `--onedir`, not `--onefile`.** One-file builds unpack to a temp folder on every
  start (slow with ~400 MB), and `config.yaml`/`data`/`logs` would land in the
  temp folder. Jarvis reads `config.yaml` next to `main.py`; in a onedir
  build that's `dist\Jarvis\_internal\`. Keep your edited copy there, or pass
  `--config` with a full path.
- **Tool discovery:** tools are found by importing every module in `tools/`
  at runtime, which PyInstaller can't see. `--collect-submodules tools`
  (and `core`, `ui`) is required, or Jarvis starts with no tools.
- **Model files aren't bundled:**
  - The Whisper model downloads to the Hugging Face cache on first run. To
    ship it, set `stt.download_root` to a folder next to the exe and copy
    the model there.
  - openWakeWord's models download into its package folder; the
    `--collect-data openwakeword` line includes them if you ran Jarvis once
    before building.
  - The Silero VAD model is inside faster-whisper's data
    (`--collect-data faster_whisper`).
- **CUDA:** a GPU build needs the CUDA/cuDNN DLLs on the target machine. The
  default CPU int8 path doesn't.
- **Antivirus** sometimes flags unsigned PyInstaller apps, especially ones
  that hook the keyboard. Code-sign the exe or add an exclusion.
- **Startup:** for an exe, point the Task Scheduler action at `Jarvis.exe
  --wake --tray` (edit the task in Task Scheduler, or adapt `core/startup.py`).
- **tkinter (overlay)** is bundled automatically. pystray needs the
  `pystray._win32` hidden import shown above.
