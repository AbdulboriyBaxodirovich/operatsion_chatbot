const chatMessages = document.getElementById('chat-messages');
const userInput = document.getElementById('user-input');
const sendBtn = document.getElementById('send-btn');

let history = [];

// Xabarni chatga qo'shish
function addMessage(text, sender) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${sender}`;
    messageDiv.textContent = text;
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// Javobni yuborish
async function sendMessage() {
    const question = userInput.value.trim();
    if (!question) return;

    // Foydalanuvchi xabarini ko'rsatish
    addMessage(question, 'user');
    userInput.value = '';

    // Loading ko'rsatish
    const loadingDiv = document.createElement('div');
    loadingDiv.className = 'message bot';
    loadingDiv.textContent = 'Yuklanmoqda...';
    chatMessages.appendChild(loadingDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    try {
        const response = await fetch('http://localhost:8080/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: question })
        });

        const data = await response.json();
        
        // Loadingni o'chirish
        chatMessages.removeChild(loadingDiv);
        
        // Bot javobini ko'rsatish
        addMessage(data.answer, 'bot');
        
        // Tarixni saqlash (keyinchalik ishlatish uchun)
        history.push({ user: question, bot: data.answer });

    } catch (error) {
        chatMessages.removeChild(loadingDiv);
        addMessage("Server bilan bog'lanishda xatolik yuz berdi. Iltimos, qayta urinib ko'ring.", 'bot');
    }
}

// Enter tugmasi bilan yuborish
userInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendMessage();
});

sendBtn.addEventListener('click', sendMessage);

// Birinchi salomlashuv
window.onload = () => {
    addMessage("Assalomu alaykum! BRB Bank yordamchisiga xush kelibsiz. Qanday yordam bera olaman?", 'bot');
};
