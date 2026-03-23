
from jinja2 import Environment, BaseLoader

class Renderer:
    def __init__(self):
        self.env = Environment(loader=BaseLoader())
        
    def render(self, template_str: str, context: dict) -> str:
        """
        Renders a Jinja2 template string with the provided context.
        """
        template = self.env.from_string(template_str)
        return template.render(**context)

    def render_story_string(self, 
                            system_prompt: str, 
                            character_name: str, 
                            character_desc: str, 
                            scenario: str, 
                            world_info: str,
                            persona: str,
                            chat_history: list[dict],
                            user_name: str,
                            user_input: str,
                            mes_example: str = "",
                            post_history_instructions: str = "") -> str:
        """
        Builds the standard Tavern-style prompt.
        """
        # Default template similar to SillyTavern's default
        default_template = """
<|system|>
{{ system_prompt }}

【角色设定】
名字：{{ character_name }}
描述：{{ character_desc }}
性格：{{ persona }}

【世界设定】
{{ world_info }}

【当前场景】
{{ scenario }}
{% if mes_example %}

<START>
{{ mes_example }}
{% endif %}
{% if chat_history %}

【历史对话】
{{ chat_history }}
{% endif %}
{% if post_history_instructions %}

【补充指令】
{{ post_history_instructions }}
{% endif %}

<|user|>
{{ user_name }}: {{ user_input }}

<|model|>
{{ character_name }}: """
        
        # Build history string
        history_text = ""
        for msg in chat_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            name = str(msg.get("name", "")).strip()
            if not name:
                name = user_name if role == "user" else character_name
            history_text += f"{name}: {content}\n"
            
        context = {
            "system_prompt": system_prompt,
            "character_name": character_name,
            "character_desc": character_desc,
            "persona": persona,
            "world_info": world_info,
            "scenario": scenario,
            "user_name": user_name,
            "user_input": user_input,
            "chat_history": history_text.strip(),
            "mes_example": mes_example,
            "post_history_instructions": post_history_instructions,
        }
        
        return self.render(default_template, context)
