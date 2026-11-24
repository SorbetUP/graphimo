import threading
import urllib.request
from http.server import ThreadingHTTPServer

from graphimo.workbench.server import WorkbenchController, WorkbenchHandler


def test_standalone_workbench_serves_renderer_and_reports_no_project_state():
    controller = WorkbenchController()
    handler = type("TestWorkbenchHandler", (WorkbenchHandler,), {"controller": controller})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/") as response:
            page = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/app.js") as response:
            script = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/style.css") as response:
            styles = response.read().decode("utf-8")
        assert "Graphimo Workbench" in page
        assert "add_palette_item" in script
        assert "inspect_artifact_store" in script
        assert 'id="artifact-store"' in page
        assert "canvas-viewport" in page
        assert "grid-template-columns" in styles
    finally:
        server.shutdown()
        server.server_close()
        controller.shutdown()
