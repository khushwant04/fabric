import pytest
from pydantic import ValidationError

from app.schemas import StampCapabilities


def test_enrollment_reports_public_url_and_gpu_fact_source():
    report = StampCapabilities(
        orchestrator="k3s",
        inference_url="https://k3s.inference.example.com/v1/",
        gpus=[{
            "product": "Tesla T4", "count": 1, "memory_bytes": 16 * 1024**3,
            "compute_capability": "7.5", "source": "node label",
        }],
    )
    assert report.inference_url == "https://k3s.inference.example.com/v1"
    assert report.gpus[0].source == "node label"
    assert StampCapabilities(orchestrator="k3s").inference_url is None


@pytest.mark.parametrize("url", [
    "http://internal.default.svc", "https://user:password@inference.example.com",
    "https://inference.example.com/?token=value", "https://inference.example.com/#secret",
    "https://inference.example.com/admin", "javascript:alert(1)",
])
def test_enrollment_refuses_credential_bearing_or_non_gateway_addresses(url):
    with pytest.raises(ValidationError):
        StampCapabilities(orchestrator="k3s", inference_url=url)
