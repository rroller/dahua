#!/usr/bin/env python3
"""
Test script for privacy mode functionality
"""
import asyncio
import aiohttp
import ssl
from custom_components.dahua.rpc2 import DahuaRpc2Client

async def test_privacy_mode():
    """Test the privacy mode functionality"""
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
            print("Testing RPC2 login...")
            await client.login()
            print("✓ RPC2 login successful")
            
            # Test get privacy mode config
            print("\nTesting get privacy mode config...")
            config = await client.get_privacy_mode_config()
            print(f"✓ Privacy mode config: {config}")
            
            # Test set privacy mode
            print("\nTesting set privacy mode...")
            result = await client.set_privacy_mode(True)
            print(f"✓ Set privacy mode result: {result}")
            
            # Test logout
            print("\nTesting logout...")
            await client.logout()
            print("✓ Logout successful")
            
        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    print("Privacy Mode Test Script")
    print("=" * 30)
    print("Make sure to update the credentials in the script!")
    print()
    
    asyncio.run(test_privacy_mode())