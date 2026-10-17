# Macro Recorder :poland: :dumpling:

Simple mouse and keyboard macro recorder for automation purposes.  
Polish interface for my own convenience :)

## Build & run 

```shell
py -m venv .venv
.venv\Scripts\activate
pip install -r .\requirements.txt
py macro_recorder.py
```

## Run as .exe
Download available on [releases page](https://github.com/Corson/macro-recorder/releases).

Alternatively, prepare .exe by running (pyinstaller required):  
`pyinstaller --onefile --noconsole --name MacroRecorder macro_recorder.py`
