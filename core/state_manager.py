import json
import os
import shutil
from config import STATE_FILE_PATH
from utils.logger import logger

class StateManager:
    def __init__(self):
        self.state = {
            "mode": "STARTUP", 
            "p_center": 0, 
            "p_gap": 0, 
            "atr_value": 0,
            "total_balance": 0.0,
            "last_long_line": None, 
            "last_short_line": None
        }
        self._load_state()

    def _load_state(self):
        if not os.path.exists(STATE_FILE_PATH): return
        try:
            with open(STATE_FILE_PATH, 'r') as f:
                self.state.update(json.load(f))
            logger.info(f"상태 로드: {self.state}")
        except Exception as e:
            logger.error(f"상태 파일 손상/로드 실패: {e}")

    def save_state(self):
        """Atomic Write: tmp 파일 작성 후 rename"""
        tmp_path = f"{STATE_FILE_PATH}.tmp"
        try:
            with open(tmp_path, 'w') as f:
                json.dump(self.state, f)
            os.rename(tmp_path, STATE_FILE_PATH)
        except Exception as e:
            logger.error(f"상태 저장 실패: {e}")

    def update(self, key, value):
        self.state[key] = value
        self.save_state()

    def get(self, key, default=None):
        return self.state.get(key, default)

    def reset_game(self, c, g, b):
        self.state.update({
            "mode": "NORMAL",
            "p_center": c,
            "p_gap": g,
            "total_balance": b,
            "last_long_line": None,
            "last_short_line": None
        })
        self.save_state()

bot_state = StateManager()
