import re

with open('/home/user1/crm_bot/frontend/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Tailwind Config
content = re.sub(
    r"theme: {\s*extend: {\s*colors: {\s*theme: {.*?}.*?}\s*}",
    """theme: {
                extend: {
                    colors: {
                        theme: {
                            bg: '#f0f4ff',
                            card: 'rgba(255,255,255,0.55)',
                            accent: '#007AFF',
                            text: '#1a1a2e',
                            muted: '#475569',
                            incomingBg: 'rgba(255,255,255,0.35)',
                            border: 'rgba(0,0,0,0.06)'
                        }
                    }
                }""",
    content, flags=re.DOTALL
)

# Replace class="dark" with class="light"
content = content.replace('<html lang="en" class="dark">', '<html lang="en" class="light">')

# 2. CSS Changes
# body
content = re.sub(
    r"body \{.*?\}",
    """body {
            background: linear-gradient(135deg, #f0f4ff 0%, #fce4ec 30%, #e8f5e9 60%, #fff3e0 100%);
            background-attachment: fixed;
            color: #1a1a2e;
            overflow: hidden;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            -webkit-font-smoothing: antialiased;
        }""",
    content, flags=re.DOTALL
)

# mesh-bg
content = re.sub(
    r"\.mesh-bg::before \{.*?\}",
    """.mesh-bg::before {
            content: '';
            position: fixed;
            inset: 0;
            z-index: 0;
            background:
                radial-gradient(circle at 20% 30%, rgba(0, 122, 255, 0.15) 0%, transparent 50%),
                radial-gradient(circle at 80% 20%, rgba(175, 82, 222, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 60% 80%, rgba(52, 199, 89, 0.1) 0%, transparent 50%),
                radial-gradient(circle at 10% 85%, rgba(255, 149, 0, 0.1) 0%, transparent 50%);
            opacity: 0.8;
            pointer-events: none;
        }""",
    content, flags=re.DOTALL
)

# glass-panel
content = re.sub(
    r"\.glass-panel \{.*?\}",
    """.glass-panel {
            background: rgba(255, 255, 255, 0.55);
            backdrop-filter: blur(16px) saturate(180%);
            -webkit-backdrop-filter: blur(16px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.5);
            box-shadow: 0 4px 16px rgba(0,0,0,0.06);
            border-radius: 1.5rem;
        }""",
    content, flags=re.DOTALL
)

# glass-card
content = re.sub(
    r"\.glass-card \{.*?\}",
    """.glass-card {
            background: rgba(255, 255, 255, 0.55);
            backdrop-filter: blur(16px) saturate(180%);
            -webkit-backdrop-filter: blur(16px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.5);
            box-shadow: 0 4px 16px rgba(0,0,0,0.06);
            border-radius: 1.5rem;
            transition: all 0.3s ease;
        }""",
    content, flags=re.DOTALL
)

content = re.sub(
    r"\.glass-card:hover \{.*?\}",
    """.glass-card:hover {
            border-color: rgba(255, 255, 255, 0.8);
            background: rgba(255, 255, 255, 0.65);
            transform: translateY(-2px);
            box-shadow: 0 8px 32px rgba(0,0,0,0.08);
        }""",
    content, flags=re.DOTALL
)

# Add solid-card for peer review optimization
if '.solid-card' not in content:
    content = content.replace('.glass-card:hover {', """.solid-card {
            background: rgba(255, 255, 255, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.5);
            box-shadow: 0 4px 16px rgba(0,0,0,0.04);
            border-radius: 1.5rem;
            transition: all 0.3s ease;
        }
        .solid-card:hover {
            border-color: rgba(255, 255, 255, 0.8);
            transform: translateY(-2px);
            box-shadow: 0 8px 32px rgba(0,0,0,0.08);
        }
        .glass-card:hover {""")

# glass-input
content = re.sub(
    r"\.glass-input \{.*?\}",
    """.glass-input {
            background: rgba(255, 255, 255, 0.35);
            backdrop-filter: blur(12px) saturate(150%);
            -webkit-backdrop-filter: blur(12px) saturate(150%);
            border: 1px solid rgba(0,0,0,0.06);
            color: #1a1a2e;
            border-radius: 9999px;
            transition: all 0.2s ease;
        }""",
    content, flags=re.DOTALL
)
content = re.sub(
    r"\.glass-input:focus \{.*?\}",
    """.glass-input:focus {
            border-color: #007AFF;
            box-shadow: 0 0 0 3px rgba(0,122,255,0.12);
            outline: none;
        }""",
    content, flags=re.DOTALL
)

# glass-btn
content = re.sub(
    r"\.glass-btn \{.*?\}",
    """.glass-btn {
            background: #007AFF;
            color: white;
            border: none;
            border-radius: 9999px;
            transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(0, 122, 255, 0.25);
            font-weight: 600;
        }""",
    content, flags=re.DOTALL
)

content = re.sub(
    r"\.glass-btn:hover \{.*?\}",
    """.glass-btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px rgba(0, 122, 255, 0.35);
        }""",
    content, flags=re.DOTALL
)

# glass-nav
content = re.sub(
    r"\.glass-nav \{.*?\}",
    """.glass-nav {
            background: rgba(255, 255, 255, 0.72);
            backdrop-filter: blur(24px) saturate(200%);
            -webkit-backdrop-filter: blur(24px) saturate(200%);
            border-top: 1px solid rgba(255, 255, 255, 0.7);
            border-radius: 9999px;
            box-shadow: 0 -4px 16px rgba(0,0,0,0.04);
        }""",
    content, flags=re.DOTALL
)

# active glow -> remove drop-shadow
content = re.sub(
    r"#nav-leads\.text-theme-accent,.*?filter: drop-shadow\(0 0 10px rgba\(255,255,255,0\.3\)\);\s*\}",
    """#nav-leads.text-theme-accent, 
        #nav-accounts.text-theme-accent, 
        #nav-ai.text-theme-accent, 
        #nav-operators.text-theme-accent {
            /* Active state styles if needed, no glow in light theme */
        }""",
    content, flags=re.DOTALL
)

# Messages
content = re.sub(
    r"\.msg-out \{.*?\}",
    """.msg-out {
            background: rgba(0, 122, 255, 0.1);
            border: 1px solid rgba(0, 122, 255, 0.15);
            color: #1a1a2e;
            box-shadow: 0 2px 8px rgba(0,0,0,0.02);
        }""",
    content, flags=re.DOTALL
)

content = re.sub(
    r"\.msg-in \{.*?\}",
    """.msg-in {
            background: rgba(255, 255, 255, 0.7);
            border: 1px solid rgba(0, 0, 0, 0.04);
            color: #1a1a2e;
            box-shadow: 0 2px 8px rgba(0,0,0,0.02);
        }""",
    content, flags=re.DOTALL
)

# Animations shimmer & glow
content = re.sub(r"@keyframes shimmer-white \{.*?\}", "", content, flags=re.DOTALL)
content = re.sub(r"\.shimmer-blue \{.*?\}", "", content, flags=re.DOTALL)
content = re.sub(r"\.shimmer-white \{.*?\}", "", content, flags=re.DOTALL)

content = re.sub(
    r"\.glow-accent \{.*?\}",
    """.glow-accent {
            box-shadow: 0 4px 12px rgba(0,122,255,0.25);
        }""",
    content, flags=re.DOTALL
)

# glass-toggle
content = re.sub(
    r"\.glass-toggle \{.*?\}",
    """.glass-toggle {
            background: rgba(0,0,0,0.08);
            border: 1px solid rgba(0,0,0,0.04);
        }""",
    content, flags=re.DOTALL
)

content = re.sub(
    r"\.peer:checked ~ \.glass-toggle \{.*?\}",
    """.peer:checked ~ .glass-toggle {
            background: #007AFF;
            border-color: #007AFF;
            box-shadow: 0 2px 8px rgba(0,122,255,0.2);
        }""",
    content, flags=re.DOTALL
)

# HTML / JS literals changes
# Header
content = content.replace('<span class="shimmer-blue">iraettae\'s</span> <span class="shimmer-white">CRM</span>', 
                          '<span class="bg-clip-text text-transparent bg-gradient-to-r from-blue-600 to-indigo-600">iraettae\'s</span> <span class="text-[#1a1a2e]">CRM</span>')

content = content.replace("bg-gradient-to-tr from-gray-700 to-gray-800", "bg-gradient-to-br from-blue-400 to-blue-600")

# text-white to text-theme-text (or text-[#1a1a2e]) in multiple places where it makes sense, but let's be careful.
# we can replace 'text-white' with 'text-[#1a1a2e]' across the whole file except in glass-btn.
# Or just replace the specific occurrences in JS render functions and specific headers.
# It's safer to use regex for class="... text-white ..." in HTML and string templates.
content = re.sub(r'(class="[^"]*)text-white([^"]*")', r'\1text-[#1a1a2e]\2', content)
content = re.sub(r"(class='[^']*)text-white([^']*')", r"\1text-[#1a1a2e]\2", content)
# But buttons should still be white text:
content = content.replace('glass-btn text-[#1a1a2e]', 'glass-btn text-white')
content = content.replace('text-[#1a1a2e] bg-theme-accent', 'text-white bg-theme-accent')
content = content.replace('text-[#1a1a2e] font-medium msg-out', 'text-[#1a1a2e] font-medium msg-out') # Wait, msg-out text is dark. That's fine.

# Replace bad red badge
content = content.replace('bg-red-500/20 text-red-400 border border-red-500/30', 'bg-red-50 text-red-600 border border-red-100')
content = content.replace('bg-red-500/20 text-red-400 border border-red-500/50', 'bg-red-50 text-red-600 border border-red-100')

# Text color in badges, labels, headings
# For headings
content = content.replace('text-[#1a1a2e] font-bold mx-auto text-white', 'text-[#1a1a2e] font-bold mx-auto') # fix double classes if any
content = content.replace('text-gray-200', 'text-[#1a1a2e]')

# Optimization: use solid-card instead of glass-card for chats list items
content = content.replace('class="flex items-start gap-3 p-3 mb-2 rounded-2xl glass-card cursor-pointer"', 
                          'class="flex items-start gap-3 p-3 mb-2 rounded-2xl solid-card cursor-pointer"')

# JS tg colors
content = content.replace("tg.setHeaderColor('#07071a');", "tg.setHeaderColor('#f0f4ff');")
content = content.replace("tg.setBackgroundColor('#07071a');", "tg.setBackgroundColor('#f0f4ff');")

# "hover:text-white" -> "hover:text-[#1a1a2e]"
content = content.replace('hover:text-white', 'hover:text-[#1a1a2e]')
content = content.replace('hover:border-white/20', 'hover:border-black/10')
content = content.replace('border-white/10', 'border-black/5')
content = content.replace('peer-checked:after:border-white', 'peer-checked:after:border-[#1a1a2e]')

# For AI paused badge in chat list:
content = content.replace("class='hidden text-[10px] bg-red-500/20 text-red-400 border border-red-500/30 px-1.5 rounded'", "class='hidden text-[10px] bg-red-50 text-red-600 border border-red-100 px-1.5 rounded'")

with open('/home/user1/crm_bot/frontend/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("Done replacing.")
