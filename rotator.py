#!/usr/bin/env python3
"""
Qoder Account Rotator
Automatically rotate through 115 Qoder accounts when rate limit is hit
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from auth_injector import QoderAuthInjector

class QoderRotator:
    def __init__(self):
        self.injector = QoderAuthInjector()
        self.state_file = Path.home() / "qoder-token-gen" / "rotation_state.json"
        self.load_state()
    
    def load_state(self):
        """Load rotation state"""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                self.state = json.load(f)
        else:
            self.state = {
                "current_index": 0,
                "total_accounts": 115,
                "last_rotation": None,
                "rotation_count": 0,
                "daily_requests": 0,
                "last_reset_date": None
            }
            self.save_state()
    
    def save_state(self):
        """Save rotation state"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w') as f:
            json.dump(self.state, indent=2, fp=f)
    
    def check_daily_reset(self):
        """Check if we need to reset daily counters"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("last_reset_date") != today:
            print(f"🔄 Daily reset: {self.state.get('last_reset_date')} → {today}")
            self.state["daily_requests"] = 0
            self.state["last_reset_date"] = today
            self.state["current_index"] = 0  # Start from first account
            self.save_state()
    
    def rotate_to_next_account(self):
        """Rotate to the next available account"""
        self.check_daily_reset()
        
        tokens = self.injector.get_9router_tokens()
        if not tokens:
            print("❌ No tokens available in 9Router")
            return False
        
        # Move to next account
        next_index = (self.state["current_index"] + 1) % len(tokens)
        
        print(f"🔄 Rotating: Account {self.state['current_index']} → {next_index}")
        
        # Inject new token
        success = self.injector.inject_token(next_index)
        
        if success:
            self.state["current_index"] = next_index
            self.state["last_rotation"] = datetime.now().isoformat()
            self.state["rotation_count"] += 1
            self.state["daily_requests"] = 0  # Reset counter for new account
            self.save_state()
            
            token = tokens[next_index]
            print(f"✅ Now using: {token['name']} (Account {next_index}/{len(tokens)-1})")
            print(f"   User ID: {token['userId']}")
            return True
        
        return False
    
    def get_current_account_info(self):
        """Get current account information"""
        tokens = self.injector.get_9router_tokens()
        if not tokens:
            return None
        
        current_index = self.state["current_index"]
        if current_index >= len(tokens):
            current_index = 0
            self.state["current_index"] = 0
            self.save_state()
        
        return tokens[current_index]
    
    def print_status(self):
        """Print current rotation status"""
        self.check_daily_reset()
        
        current = self.get_current_account_info()
        tokens = self.injector.get_9router_tokens()
        
        print("=" * 60)
        print("Qoder Account Rotation Status")
        print("=" * 60)
        
        if current:
            print(f"\n📍 Current Account: {self.state['current_index']}/{len(tokens)-1}")
            print(f"   Name: {current['name']}")
            print(f"   User ID: {current['userId']}")
        
        print(f"\n📊 Statistics:")
        print(f"   Total Accounts: {len(tokens)}")
        print(f"   Rotation Count: {self.state['rotation_count']}")
        print(f"   Daily Requests: {self.state['daily_requests']}/200")
        print(f"   Last Rotation: {self.state.get('last_rotation', 'Never')}")
        print(f"   Last Reset: {self.state.get('last_reset_date', 'Never')}")
        
        print(f"\n🔄 Available Accounts:")
        remaining = len(tokens) - self.state['current_index'] - 1
        total_remaining_requests = remaining * 200 + (200 - self.state['daily_requests'])
        print(f"   Remaining Today: {remaining} accounts")
        print(f"   Total Remaining Requests: ~{total_remaining_requests}")
        
        print("=" * 60)
    
    def run_with_rotation(self, command):
        """Run qodercli command with automatic rotation on rate limit"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Run qodercli command
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                
                # Check for rate limit error
                if result.returncode != 0:
                    error_output = result.stderr + result.stdout
                    
                    # Check for rate limit patterns
                    rate_limit_patterns = [
                        "rate limit",
                        "429",
                        "too many requests",
                        "quota exceeded",
                        "daily limit"
                    ]
                    
                    is_rate_limit = any(pattern in error_output.lower() for pattern in rate_limit_patterns)
                    
                    if is_rate_limit:
                        print(f"\n⚠️  Rate limit hit! Rotating to next account...")
                        self.state["daily_requests"] = 200  # Mark as exhausted
                        self.save_state()
                        
                        if self.rotate_to_next_account():
                            retry_count += 1
                            print(f"🔄 Retrying with new account (attempt {retry_count}/{max_retries})...")
                            continue
                        else:
                            print("❌ Failed to rotate to next account")
                            return False
                    else:
                        # Other error
                        print(f"❌ Command failed: {error_output}")
                        return False
                
                # Success
                print(result.stdout, end='')
                self.state["daily_requests"] += 1
                self.save_state()
                return True
                
            except subprocess.TimeoutExpired:
                print("⏱️  Command timed out")
                return False
            except Exception as e:
                print(f"❌ Error: {e}")
                return False
        
        print(f"❌ Max retries ({max_retries}) reached")
        return False

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Qoder Account Rotator')
    parser.add_argument('action', choices=['status', 'rotate', 'run', 'reset'], 
                       help='Action to perform')
    parser.add_argument('--command', type=str, 
                       help='Command to run with rotation (for run action)')
    parser.add_argument('--index', type=int, 
                       help='Specific account index to use (for rotate action)')
    
    args = parser.parse_args()
    
    rotator = QoderRotator()
    
    if args.action == 'status':
        rotator.print_status()
    
    elif args.action == 'rotate':
        if args.index is not None:
            rotator.state["current_index"] = args.index - 1  # Adjust for next rotation
            rotator.save_state()
        rotator.rotate_to_next_account()
    
    elif args.action == 'run':
        if not args.command:
            print("❌ --command required for run action")
            sys.exit(1)
        rotator.run_with_rotation(args.command)
    
    elif args.action == 'reset':
        rotator.state["current_index"] = 0
        rotator.state["daily_requests"] = 0
        rotator.state["rotation_count"] = 0
        rotator.save_state()
        print("✅ Reset to first account")

if __name__ == "__main__":
    main()
