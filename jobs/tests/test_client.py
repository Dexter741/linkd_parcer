import pytest
import requests

from jobs.linkedin.client import ClientConfig, LinkedInClient, LinkedInError, RateLimited


class FakeResponse:
    def __init__(self, status, text="", headers=None, url="https://www.linkedin.com/x", history=()):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.history = list(history)


class FakeSession(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(responses, **config):
    sleeps = []
    session = FakeSession(responses)
    client = LinkedInClient(
        ClientConfig(max_retries=2, backoff_base=10, **config), session=session, sleep=sleeps.append
    )
    return client, session, sleeps


def test_search_passes_params_and_start():
    client, session, _ = make_client([FakeResponse(200, "<li></li>")])
    assert client.search_page({"keywords": "python"}, 20) == "<li></li>"
    assert session.calls[0][1] == {"keywords": "python", "start": "20"}


def test_400_past_last_page_means_empty():
    client, _, _ = make_client([FakeResponse(400)])
    assert client.search_page({}, 1000) == ""


def test_detail_404_means_gone():
    client, _, _ = make_client([FakeResponse(404)])
    assert client.job_detail(1) is None


def test_retries_then_succeeds_with_backoff():
    client, _, sleeps = make_client([FakeResponse(429), FakeResponse(999), FakeResponse(200, "ok")])
    assert client.job_detail(1) == "ok"
    backoffs = [s for s in sleeps if s >= 8]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]
    assert client.requests_made == 3


def test_persistent_throttling_raises_rate_limited():
    client, _, _ = make_client([FakeResponse(429)] * 3)
    with pytest.raises(RateLimited):
        client.job_detail(1)


def test_network_errors_raise_linkedin_error():
    client, _, _ = make_client([requests.ConnectionError("boom")] * 3)
    with pytest.raises(LinkedInError) as exc:
        client.job_detail(1)
    assert not isinstance(exc.value, RateLimited)


def test_unexpected_status_raises():
    client, _, _ = make_client([FakeResponse(403)])
    with pytest.raises(LinkedInError):
        client.job_detail(1)


def test_retry_after_header_is_respected():
    client, _, sleeps = make_client([FakeResponse(429, headers={"Retry-After": "120"}), FakeResponse(200, "ok")])
    client.job_detail(1)
    assert max(sleeps) == 120  # waits Retry-After instead of the shorter backoff, not both


def test_login_wall_redirect_counts_as_throttling():
    wall = FakeResponse(
        200,
        "<h1>Join LinkedIn</h1>",
        url="https://www.linkedin.com/authwall?trk=x",
        history=[FakeResponse(302, headers={"Location": "https://www.linkedin.com/authwall?trk=x"})],
    )
    client, _, _ = make_client([wall] * 3)
    with pytest.raises(RateLimited):
        client.company_page("https://www.linkedin.com/company/acme/")


def test_uas_login_redirect_counts_as_throttling():
    wall = FakeResponse(200, "", url="https://www.linkedin.com/uas/login?session_redirect=x")
    client, _, _ = make_client([wall] * 3)
    with pytest.raises(RateLimited):
        client.job_detail(1)


def test_company_slug_that_looks_like_login_path_is_not_a_wall():
    page = FakeResponse(200, "ok", url="https://www.linkedin.com/company/signupgenius/")
    client, _, _ = make_client([page])
    assert client.company_page("https://www.linkedin.com/company/signupgenius/") == "ok"


def test_detail_400_is_an_error_not_a_page():
    client, _, _ = make_client([FakeResponse(400, "bad")])
    with pytest.raises(LinkedInError):
        client.job_detail(1)


def test_pacing_between_requests():
    client, _, sleeps = make_client([FakeResponse(200), FakeResponse(200)], min_delay=3, max_delay=3)
    client.job_detail(1)
    client.job_detail(2)
    assert len(sleeps) == 1 and 2.5 < sleeps[0] <= 3
