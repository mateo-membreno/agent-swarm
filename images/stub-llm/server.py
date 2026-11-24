"""A deterministic stand-in for an OpenAI-compatible chat server. For demos and CI only: it does no inference."""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def reply(prompt: str) -> str:
    fields = re.search(r"Reply with ONLY a JSON object.*?Fields: ([^\n]+)", prompt, re.S)
    if fields:                                     # the built-in LLM agent: answer in the shape it asked for
        title = (re.search(r'"title": "([^"]*)"', prompt) or [None, "this story"])[1]
        out = {}
        for f in (x.strip() for x in fields[1].split(",")):
            out[f] = f"Stub summary of: {title}"[:190] if f == "summary" else f"stub-{f}"[:30]
        return json.dumps(out)
    return "(stub answer)"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if body.get("model") == "__fail__" or any("__FAIL__" in m.get("content", "") for m in body.get("messages", [])):
            self.send_response(500)
            self.end_headers()
            return
        time.sleep(0.2)
        text = reply(body["messages"][-1]["content"])
        prompt_tokens = len(body["messages"][-1]["content"]) // 4
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                          "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(text) // 4}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
