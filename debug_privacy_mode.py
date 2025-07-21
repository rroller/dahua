#!/usr/bin/env python3
"""
Debug script for privacy mode functionality
"""
import asyncio
import aiohttp
import ssl
import json
from custom_components.dahua.rpc2 import DahuaRpc2Client

async def debug_privacy_mode():
    """Debug the privacy mode functionality"""
    # SSL context for self-signed certificates
    ssl_context = ssl.create_default_context()
    ssl_context.set_ciphers("DEFAULT")
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        # Replace these with your camera credentials
        username = "admin"
        password = "your_password"
        address = "your_camera_ip"
        port = 80
        rtsp_port = 554
        
        client = DahuaRpc2Client(username, password, address, port, rtsp_port, session)
        
        try:
            # Test login
            print("Step 1: Testing RPC2 login...")
            login_response = await client.login()
            print(f"✓ Login response: {json.dumps(login_response, indent=2)}")
            
            # Test get privacy mode config (initial state)
            print("\nStep 2: Getting initial privacy mode config...")
            config = await client.get_privacy_mode_config()
            print(f"✓ Initial config: {json.dumps(config, indent=2)}")
            
            current_state = config.get("table", [{}])[0].get("Enable", False)
            print(f"Current privacy mode state: {current_state}")
            
            # Test set privacy mode to opposite state
            new_state = not current_state
            print(f"\nStep 3: Setting privacy mode to {new_state}...")
            
            # Let's manually make the request to see the raw response
            params = {
                "name": "LeLensMask",
                "table": [{
                    "Enable": new_state,
                    "LastPosition": [-0.5861111111111111, -0.2061111111111111, 0.0078125],
                    "TimeSection": [
                        ["1 00:00:00-23:59:59", "0 00:00:00-23:59:59", "0 00:00:00-23:59:59", 
                         "0 00:00:00-23:59:59", "0 00:00:00-23:59:59", "0 00:00:00-23:59:59"]
                    ] * 7
                }],
                "options": []
            }
            
            print(f"Request params: {json.dumps(params, indent=2)}")
            
            response = await client.request(method="configManager.setConfig", params=params)
            print(f"✓ Set privacy mode response: {json.dumps(response, indent=2)}")
            
            # Test get privacy mode config (after change)
            print("\nStep 4: Getting privacy mode config after change...")
            config_after = await client.get_privacy_mode_config()
            print(f"✓ Config after change: {json.dumps(config_after, indent=2)}")
            
            new_current_state = config_after.get("table", [{}])[0].get("Enable", False)
            print(f"New privacy mode state: {new_current_state}")
            
            if new_current_state == new_state:
                print("✓ Privacy mode change successful!")
            else:
                print("✗ Privacy mode change failed!")
            
            # Test logout
            print("\nStep 5: Testing logout...")
            logout_response = await client.logout()
            print(f"✓ Logout response: {json.dumps(logout_response, indent=2)}")
            
        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    print("Privacy Mode Debug Script")
    print("=" * 40)
    print("Make sure to update the credentials in the script!")
    print()
    
    asyncio.run(debug_privacy_mode())