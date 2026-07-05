import re

with open('frontend/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add view
view_html = """
    <!-- VIEW: TEST AI -->
    <div id="view-test-ai" class="absolute inset-0 flex flex-col pb-28 z-[1] hidden">
        <div class="px-5 pt-5 pb-3 flex items-center justify-between">
            <h1 class="text-xl font-bold text-[#1a1a2e]">🧪 Тест ИИ</h1>
            <button onclick="createTestChat()" class="glass-btn text-white w-8 h-8 rounded-full flex items-center justify-center font-bold text-lg leading-none">+</button>
        </div>
        <div class="px-5 mb-4 text-theme-muted text-[13px] leading-relaxed">
            Создавайте тестовые чаты, чтобы проверить работу автоответчика без реальных лидов.
        </div>
        <div id="test-chats-list" class="flex-1 overflow-y-auto scrollbar-hide px-5 gap-3 flex flex-col pb-4">
            <div class="text-center text-theme-muted text-xs mt-10">Загрузка...</div>
        </div>
    </div>

    <!-- VIEW: TEST AI CHAT WINDOW -->
    <div id="view-test-ai-chat" class="absolute inset-0 flex flex-col z-30 hidden translate-x-full transition-transform duration-300 bg-theme-bg">
        <!-- Chat Header -->
        <div class="px-2 pt-4 pb-3 flex items-center gap-2 glass-panel z-10 border-b-0 rounded-none">
            <button onclick="closeTestChat()" class="p-2 text-[#1a1a2e] hover:bg-white/10 rounded-full transition shrink-0">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
            </button>
            <div class="w-9 h-9 bg-gradient-to-br from-green-400 to-teal-600 rounded-full flex items-center justify-center font-bold text-white text-lg relative shrink-0">
                🧪
            </div>
            <h2 class="font-semibold text-[15px] truncate text-[#1a1a2e] flex-1 min-w-0" id="test-chat-header-name">Test Chat</h2>
            <button onclick="deleteCurrentTestChat()" class="p-1.5 text-red-400 hover:text-red-500 transition rounded-full" title="Удалить чат">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
            </button>
        </div>

        <!-- Messages Area -->
        <div id="test-messages-list" class="flex-1 overflow-y-auto p-4 flex flex-col gap-4 scrollbar-hide relative">
            <div class="text-center text-xs text-theme-muted my-2"><span class="glass-card px-3 py-1 rounded-full text-theme-muted">Chat opened</span></div>
        </div>

        <div id="test-ai-loading" class="hidden px-4 pb-2 text-xs text-theme-muted flex items-center gap-2">
            <div class="w-4 h-4 border-2 border-theme-accent border-t-transparent rounded-full animate-spin"></div> ИИ думает...
        </div>

        <!-- Input Area -->
        <div class="p-3 glass-panel border-t-0 rounded-none shrink-0 mb-4 flex flex-col gap-2">
            <form id="testSendForm" class="flex items-center gap-2">
                <div class="flex-1 glass-card rounded-full flex items-center pr-1 pl-4 py-1">
                    <input type="text" id="testMessageInput" class="flex-1 bg-transparent text-[#1a1a2e] border-none outline-none py-2 placeholder-theme-muted text-[15px]" placeholder="Написать..." autocomplete="off">
                </div>
                <button type="submit" id="testSendBtn" class="w-10 h-10 glass-btn text-white rounded-full flex items-center justify-center shrink-0 hover:scale-105 transition glow-accent">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                </button>
            </form>
        </div>
    </div>
"""
content = content.replace('<!-- VIEW: CHAT WINDOW -->', view_html + '\n    <!-- VIEW: CHAT WINDOW -->')

# 2. Add nav tab
nav_html = """
        <div onclick="switchTab('test-ai')" id="nav-test-ai"
            class="flex flex-col items-center justify-center flex-1 gap-1 cursor-pointer text-theme-muted hover:text-[#1a1a2e] transition">
            <div class="nav-dot relative flex items-center justify-center">
                <div class="nav-circle"></div>
                <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M19.8 18.4L14 10.67V6.5l1.35-1.69c.26-.33.03-.81-.39-.81H9.04c-.42 0-.65.48-.39.81L10 6.5v4.17L4.2 18.4c-.49.66-.02 1.6.8 1.6h14c.82 0 1.29-.94.8-1.6zM12 11.5l3.54 4.75c.1.13.15.28.15.44H8.31c0-.16.05-.31.15-.44L12 11.5z"/>
                </svg>
            </div>
            <span class="text-[11px] font-semibold whitespace-nowrap">Тест</span>
        </div>
"""
content = content.replace('</nav>', nav_html + '    </nav>')

# 3. Add to switchTab
js_vars = """
        const viewTestAI = document.getElementById('view-test-ai');
        const navTestAI = document.getElementById('nav-test-ai');
"""
content = content.replace("const navOperators = document.getElementById('nav-operators');", "const navOperators = document.getElementById('nav-operators');\n" + js_vars)

switch_tab_hide = """
            viewTestAI.classList.add('hidden');
            navTestAI.classList.remove('text-theme-accent'); navTestAI.classList.remove('nav-active');
            navTestAI.classList.add('text-theme-muted', 'hover:text-[#1a1a2e]');
"""
content = content.replace("navOperators.classList.add('text-theme-muted', 'hover:text-[#1a1a2e]');", "navOperators.classList.add('text-theme-muted', 'hover:text-[#1a1a2e]');\n" + switch_tab_hide)

switch_tab_show = """
            } else if (tabName === 'test-ai') {
                viewTestAI.classList.remove('hidden');
                navTestAI.classList.add('text-theme-accent', 'nav-active');
                navTestAI.classList.remove('text-theme-muted', 'hover:text-[#1a1a2e]');
                loadTestChats();
"""
content = content.replace("loadOperators();\n            }", "loadOperators();\n" + switch_tab_show + "            }")

# 4. showScreen alias and Test AI Logic
test_ai_logic = """
        function showScreen(screen) {
            switchTab(screen);
        }

        // --- TEST AI LOGIC ---
        let currentTestChatId = null;
        let allTestChatsData = [];
        let testChatMessages = {};

        async function loadTestChats() {
            try {
                const res = await fetch('/api/test_ai/chats', { headers: { 'Authorization': getAuthHeader() } });
                if (!res.ok) return;
                allTestChatsData = await res.json();
                renderTestChats();
            } catch (e) { console.error(e); }
        }

        function renderTestChats() {
            const container = document.getElementById('test-chats-list');
            if (allTestChatsData.length === 0) {
                container.innerHTML = '<div class="text-center text-theme-muted text-xs mt-10">Нет тестовых чатов</div>';
                return;
            }
            container.innerHTML = allTestChatsData.map(chat => `
                <div onclick="openTestChat('${chat.id}', '${chat.name.replace(/'/g, "\\'")}')" class="flex items-center gap-3 p-3 mb-2 rounded-2xl solid-card cursor-pointer">
                    <div class="w-12 h-12 bg-gradient-to-br from-green-400 to-teal-600 rounded-full flex items-center justify-center font-bold text-white text-lg relative shrink-0 shadow-lg">
                        🧪
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="flex justify-between items-start mb-1">
                            <span class="font-bold text-[15px] truncate text-[#1a1a2e]">${chat.name}</span>
                            <span class="text-[11px] text-theme-muted font-medium shrink-0 ml-2">${chat.message_count} msg</span>
                        </div>
                        <div class="text-[13px] text-theme-muted truncate pr-2 mt-1">
                            ${chat.last_message || '...'}
                        </div>
                    </div>
                </div>
            `).join('');
        }

        async function createTestChat() {
            const name = prompt("Введите имя для тестового лида (или оставьте пустым):");
            if (name === null) return;
            try {
                await fetch('/api/test_ai/chats', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': getAuthHeader() },
                    body: JSON.stringify({ name: name || undefined })
                });
                loadTestChats();
            } catch (e) { console.error(e); }
        }

        async function deleteCurrentTestChat() {
            if (!currentTestChatId) return;
            if (!confirm("Удалить этот тестовый чат?")) return;
            try {
                await fetch(`/api/test_ai/chats/${currentTestChatId}`, { method: 'DELETE', headers: { 'Authorization': getAuthHeader() } });
                closeTestChat();
                loadTestChats();
            } catch (e) { console.error(e); }
        }

        function openTestChat(id, name) {
            currentTestChatId = id;
            document.getElementById('test-chat-header-name').innerText = name;
            const chatView = document.getElementById('view-test-ai-chat');
            chatView.classList.remove('hidden');
            setTimeout(() => { chatView.classList.remove('translate-x-full'); }, 10);
            
            if (!testChatMessages[id]) testChatMessages[id] = [];
            renderTestMessages();
        }

        function closeTestChat() {
            currentTestChatId = null;
            const chatView = document.getElementById('view-test-ai-chat');
            chatView.classList.add('translate-x-full');
            setTimeout(() => { chatView.classList.add('hidden'); }, 300);
            loadTestChats();
        }

        function renderTestMessages() {
            const container = document.getElementById('test-messages-list');
            const msgs = testChatMessages[currentTestChatId] || [];
            
            if (msgs.length === 0) {
                container.innerHTML = '<div class="text-center text-xs text-theme-muted mt-20 glass-card px-4 py-2 rounded-full w-fit mx-auto">История пуста. Напишите что-нибудь, чтобы ИИ ответил.</div>';
                return;
            }

            container.innerHTML = msgs.map(m => {
                if (m.type === 'tag') {
                    if (m.tag_type === 'escalate') {
                        return `<div class="mx-auto my-2 px-3 py-1.5 bg-red-100 border border-red-200 text-red-600 rounded-xl text-xs font-bold text-center max-w-[85%]">⚠️ ЭСКАЛАЦИЯ: ${m.reason}</div>`;
                    } else if (m.tag_type === 'lead_ready') {
                        const d = m.data || {};
                        return `<div class="mx-auto my-2 px-4 py-3 bg-green-50 border border-green-200 text-green-800 rounded-xl text-xs max-w-[85%] shadow-sm">
                            <div class="font-bold text-green-700 mb-2 flex items-center justify-center gap-1"><span class="text-sm">🚀</span> ЛИД ГОТОВ</div>
                            <table class="w-full text-left">
                                ${d.name ? `<tr><td class="pr-2 py-0.5 font-semibold text-green-600/70">Имя:</td><td class="font-medium">${d.name}</td></tr>` : ''}
                                ${d.phone ? `<tr><td class="pr-2 py-0.5 font-semibold text-green-600/70">Тел:</td><td class="font-medium">${d.phone}</td></tr>` : ''}
                                ${d.dob ? `<tr><td class="pr-2 py-0.5 font-semibold text-green-600/70">ДР:</td><td class="font-medium">${d.dob}</td></tr>` : ''}
                                ${d.citizenship ? `<tr><td class="pr-2 py-0.5 font-semibold text-green-600/70">Гражд:</td><td class="font-medium">${d.citizenship}</td></tr>` : ''}
                                ${d.transport ? `<tr><td class="pr-2 py-0.5 font-semibold text-green-600/70">Транс:</td><td class="font-medium">${d.transport}</td></tr>` : ''}
                            </table>
                        </div>`;
                    } else if (m.tag_type === 'silence') {
                        return `<div class="mx-auto my-2 px-3 py-1.5 bg-gray-100 border border-gray-200 text-gray-500 rounded-xl text-xs font-medium text-center max-w-[85%]">🤫 ИИ промолчал</div>`;
                    }
                    return '';
                }

                const isOut = m.role === 'user';
                const alignOuter = isOut ? 'self-end' : 'self-start flex-row';
                const bgClass = isOut ? 'msg-out text-[#1a1a2e] font-medium' : 'msg-in text-[#1a1a2e]';
                const radiusObj = isOut ? 'rounded-2xl rounded-tr-sm' : 'rounded-2xl rounded-tl-sm';

                return `
                <div class="message-bubble ${alignOuter} flex max-w-[88%] animate-fade-in mb-1">
                    <div class="${bgClass} ${radiusObj} py-2.5 px-3.5 relative overflow-hidden">
                        <div class="text-[14.5px] whitespace-pre-wrap">${m.text}</div>
                    </div>
                </div>
                `;
            }).join('');
            
            container.scrollTop = container.scrollHeight;
        }

        document.getElementById('testSendForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const input = document.getElementById('testMessageInput');
            const text = input.value.trim();
            if (!currentTestChatId || !text) return;

            if (!testChatMessages[currentTestChatId]) testChatMessages[currentTestChatId] = [];
            testChatMessages[currentTestChatId].push({ role: 'user', text: text });
            renderTestMessages();
            input.value = '';

            const loading = document.getElementById('test-ai-loading');
            loading.classList.remove('hidden');

            try {
                const res = await fetch(`/api/test_ai/chats/${currentTestChatId}/message`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': getAuthHeader() },
                    body: JSON.stringify({ message: text })
                });
                const data = await res.json();
                
                if (data.display) {
                    testChatMessages[currentTestChatId].push({ role: 'assistant', text: data.display });
                }
                
                if (data.tags && data.tags.length > 0) {
                    for (const tag of data.tags) {
                        testChatMessages[currentTestChatId].push({ type: 'tag', tag_type: tag.type, reason: tag.reason, data: tag.data });
                    }
                }
                
                if (data.is_silence && (!data.tags || !data.tags.find(t => t.type === 'silence'))) {
                    testChatMessages[currentTestChatId].push({ type: 'tag', tag_type: 'silence' });
                }

                renderTestMessages();
            } catch (err) {
                console.error(err);
                alert("Ошибка при отправке в тест-чат");
            } finally {
                loading.classList.add('hidden');
            }
        });
"""
content = content.replace("// Initial Data Fetch", test_ai_logic + "\n        // Initial Data Fetch")

with open('frontend/index.html', 'w', encoding='utf-8') as f:
    f.write(content)
