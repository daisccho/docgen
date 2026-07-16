from django import forms

from .models import Project


class ProjectForm(forms.ModelForm):
    llm_api_key = forms.CharField(
        label="API-ключ LLM",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Хранится в зашифрованном виде. Пустое поле сохраняет текущий ключ.",
    )
    clear_llm_api_key = forms.BooleanField(
        label="Удалить сохранённый API-ключ", required=False
    )
    github_token = forms.CharField(
        label="GitHub-токен",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Нужен для приватных репозиториев и GitHub Releases API.",
    )
    clear_github_token = forms.BooleanField(
        label="Удалить сохранённый GitHub-токен", required=False
    )

    class Meta:
        model = Project
        fields = [
            "name",
            "slug",
            "repository_url",
            "default_branch",
            "llm_model",
            "llm_base_url",
            "max_iterations",
            "watch_interval",
            "llm_provider",
        ]
        widgets = {
            "repository_url": forms.URLInput(
                attrs={"placeholder": "https://git.example.org/team/project.git"}
            )
        }

    def save(self, commit=True):
        project = super().save(commit=False)
        if self.cleaned_data.get("clear_llm_api_key"):
            project.set_llm_api_key("")
        elif self.cleaned_data.get("llm_api_key"):
            project.set_llm_api_key(self.cleaned_data["llm_api_key"])

        if self.cleaned_data.get("clear_github_token"):
            project.set_github_token("")
        elif self.cleaned_data.get("github_token"):
            project.set_github_token(self.cleaned_data["github_token"])

        if commit:
            project.save()
            self.save_m2m()
        return project
