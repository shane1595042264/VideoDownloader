#!/usr/bin/env python3
"""
VideoDownloader - Launch Script
Run this to start the server: python run.py
"""

import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("\n" + "=" * 50)
    print(f"  VideoDownloader is starting on port {port}...")
    print(f"  Open http://localhost:{port} in your browser")
    print("=" * 50 + "\n")

    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
