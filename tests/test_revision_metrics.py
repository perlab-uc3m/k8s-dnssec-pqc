import pytest


def test_metrics_ignores_terminating_rollout_pods(tmp_path, monkeypatch):
    import src.revision.metrics as metrics

    def pod(name, deleting=False):
        metadata = {"name": name, "uid": name}
        if deleting:
            metadata["deletionTimestamp"] = "2026-09-29T12:00:00Z"
        return {
            "metadata": metadata,
            "spec": {"nodeName": "one-node"},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }

    connected = []

    class Forward:
        def __init__(self, kubectl, context, namespace, name):
            self.name = name

        def __enter__(self):
            connected.append(self.name)
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(
        metrics, "_kubectl_json", lambda *args: {"items": [pod("old", True), pod("new")]}
    )
    monkeypatch.setattr(metrics, "PodForward", Forward)
    collector = metrics.Metrics("kubectl", "test", tmp_path)
    try:
        collector.start(0, expected_replicas=1)
        assert connected == ["new"]
        assert collector.pods[0]["uid"] == "new"
    finally:
        collector.close()
    with pytest.raises(RuntimeError, match="pods unavailable"):
        collector.start(0, expected_replicas=2)
