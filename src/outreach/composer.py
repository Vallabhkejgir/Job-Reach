"""Hybrid Jinja2 role-specific outreach email composer with optional LLM hook."""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Callable, Dict, Optional

from src.models import DecisionMaker, JobPosting, OutreachDraft
from src.scrapers.exp_filter import infer_role_category

logger = logging.getLogger(__name__)

ROLE_TEMPLATES: Dict[str, Dict[str, str]] = {
    "ai_engineer": {
        "role_label": "AI Engineer",
        "default_skills": "Python, PyTorch, LLM/RAG pipelines, and FastAPI",
        "default_highlight": (
            "built production-ready RAG and agentic LLM services with low-latency inference, "
            "vector retrieval evaluation, and modular FastAPI deployment"
        ),
        "value_pitch": (
            "I enjoy turning applied ML/GenAI research into reliable, measurable product features."
        ),
    },
    "data_scientist": {
        "role_label": "Data Scientist",
        "default_skills": "Python, Scikit-Learn, SQL, Pandas, and statistical modeling",
        "default_highlight": (
            "delivered end-to-end predictive ML models, automated feature engineering pipelines, "
            "and actionable experimentation dashboards"
        ),
        "value_pitch": (
            "I focus on pairing rigorous statistical analysis with clean, reproducible data engineering."
        ),
    },
    "software_developer": {
        "role_label": "Software Developer",
        "default_skills": "Python, TypeScript, REST APIs, relational databases, and automated testing",
        "default_highlight": (
            "shipped full-stack web services, modular REST APIs, and comprehensive CI test suites "
            "with a strong emphasis on maintainability"
        ),
        "value_pitch": (
            "I take pride in writing clean, well-tested code and shipping user-facing features quickly."
        ),
    },
    "backend_engineer": {
        "role_label": "Backend Engineer",
        "default_skills": "Python, FastAPI, PostgreSQL, Redis, and Docker",
        "default_highlight": (
            "designed high-throughput backend microservices, optimized SQL/Redis data layers, "
            "and built resilient asynchronous worker queues"
        ),
        "value_pitch": (
            "I love solving distributed systems, API reliability, and database performance challenges."
        ),
    },
}

SUBJECT_JINJA_TEMPLATE = (
    "{{ role_title }} Application ({{ exp_badge }}) - {{ candidate_name }} | {{ company }}"
)

BODY_JINJA_TEMPLATE = """Hi {{ greeting_name }},

I noticed {{ company }} is hiring for the {{ role_title }} position ({{ location }}) and wanted to reach out directly.

As an entry-level {{ role_label }} skilled in {{ skills_summary }}, I have {{ candidate_highlight }}. {{ custom_hook }}
{% if is_edge_case %}
While the listing mentions {{ experience_raw }}, my hands-on experience shipping production-grade {{ role_label }} systems allows me to ramp up rapidly and contribute from day one.
{% endif %}
Key highlights I can bring to {{ company }}:
- Core Stack: {{ skills_summary }}
- Recent Work: {{ candidate_highlight }}
- Portfolio & Code: {{ portfolio_url }}

I have attached my resume (and linked my profile at {{ linkedin_url }}) for your review. Would you be open to a brief 10-minute chat this week regarding the {{ role_title }} role?

Best regards,
{{ candidate_name }}
{{ candidate_email }}
{{ portfolio_url }}"""


def _render_template(template_str: str, context: Dict[str, Any]) -> str:
    """Renders a Jinja2 template using `jinja2` if installed, or a built-in Jinja-compatible renderer."""
    try:
        from jinja2 import Template

        tmpl = Template(template_str, trim_blocks=True, lstrip_blocks=True)
        return tmpl.render(**context).strip()
    except ImportError:
        # Built-in fallback supporting {% if var %}...{% endif %} and {{ var }}
        def _replace_if(match: re.Match[str]) -> str:
            var_name = match.group(1).strip()
            inner = match.group(2)
            return inner.strip("\n") if bool(context.get(var_name)) else ""

        rendered = re.sub(
            r"\{%\s*if\s+([a-zA-Z0-9_]+)\s*%\}(.*?)\{%\s*endif\s*%\}",
            _replace_if,
            template_str,
            flags=re.DOTALL,
        )
        for key, value in context.items():
            rendered = re.sub(
                r"\{\{\s*" + re.escape(key) + r"\s*\}\}",
                str(value if value is not None else ""),
                rendered,
            )
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)
        return rendered.strip()


class OutreachComposer:
    """Composes personalized cold outreach emails tailored to the job role and hiring manager."""

    def __init__(
        self,
        candidate_profile: Optional[Dict[str, Any]] = None,
        enable_llm_hook: bool = False,
        llm_hook_fn: Optional[Callable[[JobPosting, DecisionMaker], str]] = None,
    ) -> None:
        profile = candidate_profile or {}
        self.candidate_name = profile.get("name", "Alex Rivera")
        self.candidate_email = profile.get("email", "alex.rivera@example.com")
        self.portfolio_url = profile.get(
            "portfolio_url", "https://github.com/alexrivera"
        )
        self.linkedin_url = profile.get(
            "linkedin_url", "https://www.linkedin.com/in/alexrivera"
        )
        self.highlights: Dict[str, str] = profile.get("highlights") or {}
        self.enable_llm_hook = enable_llm_hook
        self.llm_hook_fn = llm_hook_fn

    def compose(
        self, job: JobPosting, decision_maker: DecisionMaker
    ) -> OutreachDraft:
        """Generates a role-tailored OutreachDraft for a job posting and decision maker."""
        role_key = job.role_category or infer_role_category(job.title)
        if role_key not in ROLE_TEMPLATES:
            role_key = infer_role_category(job.title)

        role_meta = ROLE_TEMPLATES[role_key]
        first_name = (decision_maker.first_name or "").strip()
        if not first_name or first_name.lower() in ("hiring", "talent", "unknown"):
            greeting_name = f"{job.company} Hiring Team"
        else:
            greeting_name = first_name

        skills_summary = (
            ", ".join(job.skills[:5])
            if job.skills
            else role_meta["default_skills"]
        )
        candidate_highlight = (
            self.highlights.get(role_key) or role_meta["default_highlight"]
        )

        custom_hook = role_meta["value_pitch"]
        template_used = f"jinja2_{role_key}"
        if self.enable_llm_hook and self.llm_hook_fn is not None:
            try:
                generated_hook = self.llm_hook_fn(job, decision_maker)
                if generated_hook and generated_hook.strip():
                    custom_hook = generated_hook.strip()
                    template_used = f"jinja2_{role_key}+llm_hook"
            except Exception as exc:
                logger.warning(
                    "LLM hook failed (%s); falling back to deterministic Jinja2 hook.",
                    exc,
                )

        exp_badge = (
            job.experience_raw.strip()
            if job.experience_raw
            else ("1-3 Yrs" if job.is_edge_case else "Entry-Level / 0-2 Yrs")
        )

        context = {
            "greeting_name": greeting_name,
            "company": job.company,
            "role_title": job.title,
            "location": job.location or "India",
            "role_label": role_meta["role_label"],
            "skills_summary": skills_summary,
            "candidate_highlight": candidate_highlight,
            "custom_hook": custom_hook,
            "is_edge_case": bool(job.is_edge_case),
            "experience_raw": exp_badge,
            "exp_badge": exp_badge,
            "candidate_name": self.candidate_name,
            "candidate_email": self.candidate_email,
            "portfolio_url": self.portfolio_url,
            "linkedin_url": self.linkedin_url,
        }

        subject = _render_template(SUBJECT_JINJA_TEMPLATE, context)
        body_text = _render_template(BODY_JINJA_TEMPLATE, context)
        body_html = self._text_to_html(body_text)

        return OutreachDraft(
            job_id=job.job_id,
            recipient_email=decision_maker.email or "",
            recipient_name=decision_maker.full_name,
            recipient_title=decision_maker.title,
            company=job.company,
            role_title=job.title,
            role_category=role_key,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            is_edge_case=bool(job.is_edge_case),
            template_used=template_used,
            job_url=job.job_url,
        )

    @staticmethod
    def _text_to_html(body_text: str) -> str:
        escaped = html.escape(body_text)
        paragraphs = [
            f"<p>{p.replace(chr(10), '<br/>')}</p>"
            for p in escaped.split("\n\n")
            if p.strip()
        ]
        return (
            "<div style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
            "font-size: 14px; line-height: 1.5; color: #1f2937;\">\n"
            + "\n".join(paragraphs)
            + "\n</div>"
        )
