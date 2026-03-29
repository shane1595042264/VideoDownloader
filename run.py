#!/usr/bin/env python3
"""
VideoDownloader - Launch Script
Run this to start the server: python run.py
"""

import uvicorn

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  VideoDownloader is starting...")
    print("  Open http://localhost:8000 in your browser")
    print("=" * 50 + "\n")

    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
