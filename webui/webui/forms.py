from django import forms

from .models import GlobalSettings, Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "name",
            "repository_url",
            "default_branch",
            "watch_interval",
        ]
        widgets = {
            "repository_url": forms.URLInput(
                attrs={"placeholder": "https://git.example.org/team/project.git"}
            )
        }


class GlobalSettingsForm(forms.ModelForm):
    llm_api_key = forms.CharField(
        label="API-ключ LLM",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Пустое поле сохраняет уже заданный ключ.",
    )
    clear_llm_api_key = forms.BooleanField(
        label="Удалить API-ключ", required=False
    )
    github_token = forms.CharField(
        label="GitHub-токен",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Нужен для приватных репозиториев и GitHub Releases API.",
    )
    clear_github_token = forms.BooleanField(
        label="Удалить GitHub-токен", required=False
    )

    class Meta:
        model = GlobalSettings
        fields = ["llm_model", "llm_base_url", "max_iterations"]
        widgets = {
            "llm_base_url": forms.URLInput(
                attrs={"placeholder": "Например, https://opencode.ai/zen/v1"}
            ),
            "max_iterations": forms.NumberInput(attrs={"min": 5, "max": 500}),
        }
        help_texts = {
            "llm_model": (
                "Модель применяется ко всем проектам WebUI."
            ),
            "llm_base_url": (
                "Оставьте пустым для официального OpenAI API. Для "
                "OpenAI-совместимого провайдера укажите его API endpoint."
            ),
            "max_iterations": (
                "Максимальное число ходов LLM с инструментами на один документ."
            ),
        }

    def save(self, commit=True):
        common = super().save(commit=False)
        if self.cleaned_data.get("clear_llm_api_key"):
            common.set_llm_api_key("")
        elif self.cleaned_data.get("llm_api_key"):
            common.set_llm_api_key(self.cleaned_data["llm_api_key"])
        if self.cleaned_data.get("clear_github_token"):
            common.set_github_token("")
        elif self.cleaned_data.get("github_token"):
            common.set_github_token(self.cleaned_data["github_token"])
        if commit:
            common.save()
        return common
