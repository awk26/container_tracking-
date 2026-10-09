import requests

import time
def test_get_container_status():
    response = requests.get("https://www.maersk.com/tracking/SLZU2577558")
    if response.status_code == 200:
        print("Request successful!")
        print(response.text)

test_get_container_status()        
        

