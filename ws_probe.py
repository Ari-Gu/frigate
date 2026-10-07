import websocket, time, sys

WS_URL = "ws://127.0.0.1:5001/ws"

def main():
    try:
        ws = websocket.create_connection(WS_URL, timeout=8)
    except Exception as e:
        print("CONNECT_ERROR:", e)
        return
    print("CONNECTED to", WS_URL)
    end = time.time() + 10
    count = 0
    while time.time() < end:
        try:
            ws.settimeout(2.0)
            m = ws.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except Exception as e:
            print("RECV_ERROR:", e)
            break
        count += 1
        # print first 3 raw, then only person-related
        if count <= 3:
            print(f"--- MSG #{count} (raw, first 700 chars) ---")
            print(m[:700])
        try:
            obj = __import__("json").loads(m)
            msg = obj.get("message", {})
            aft = msg.get("after", {})
            if aft.get("label") == "person":
                print("PERSON:", json.dumps({
                    "type": msg.get("type"),
                    "id": aft.get("id"),
                    "event_id": aft.get("event_id"),
                    "label": aft.get("label"),
                    "box": aft.get("box"),
                    "stationary": aft.get("stationary"),
                    "area": aft.get("area"),
                    "score": aft.get("top_score"),
                }))
        except Exception:
            pass
    print(f"DONE. total messages={count}")
    ws.close()

if __name__ == "__main__":
    main()
