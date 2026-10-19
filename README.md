# Convert Gemini Logs from Google Takeout to MD Python Script

## Purpose

- Convert Google Takeout data for personal use
- Proof of concept/Showcase of forensic work on MyActivity.json

## Prerequisites

- Python v3.14+
- Beautiful Soup 4
- Google Takeout zip file containing Gemini App My Activity. 

## Useage

`python takeout-gemini-md.py --takeoutzip takeout.zip [--out gemini_log]`

## Output

```
\gemini_log\
index.csv */simple inventory file: file name, /*
   \logs\
      YYYYMMDD_uuid1.md */threads in MD. YAML metadata header followed by thread in MD/*
      YYYYMMDD_uuid2.md
      YYYYMMDD_uuid3.md
      YYYYMMDD_uuid4.md
      YYYYMMDD_uuid4.md
      \attachments\
          attachment1.jpg  */parent thread name_index4.ext/*
          attachment2.png
          attachment3.txt
```

## Source Code

[takeout-gemini-md.py](takeout-gemini-md)

## LICENSE

[MIT License](LICENSE)

## About

Author @5ynthaire
AI Credits: Codex, Claude
