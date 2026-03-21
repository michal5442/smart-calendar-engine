// Global State
const state = {
    mandatoryParticipants: [],
    optionalParticipants: [],
    eventDurationHours: 1,
    targetDate: null,
    bufferMinutes: 15,
    availableSlots: [],
    rescheduleSuggestions: [],
    selectedSlot: null,
};

// ========================
// Initialize
// ========================
document.addEventListener('DOMContentLoaded', async () => {
    console.log('🚀 Initializing Calendar Engine UI');
    
    // הגדר תאריך מינימום (היום)
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('target-date').min = today;
    document.getElementById('target-date').value = today;
    state.targetDate = today;
    
    // טען משתתפים
    await loadParticipants();
});

// ========================
// Load Participants
// ========================
async function loadParticipants() {
    try {
        const response = await fetch('/api/participants');
        const data = await response.json();
        
        if (data.status === 'success') {
            renderParticipants(data.participants);
            console.log(`✓ טען ${data.count} משתתפים`);
        } else {
            showError('שגיאה בטעינת משתתפים: ' + data.error);
        }
    } catch (error) {
        console.error('Error loading participants:', error);
        showError('שגיאה בתקשורת עם השרת');
    }
}

// ========================
// Render Participants
// ========================
function renderParticipants(participants) {
    const container = document.getElementById('participants-container');
    container.innerHTML = '';
    
    participants.forEach(participant => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'participant-button unselected';
        button.dataset.participant = participant;
        button.dataset.status = 'unselected'; // unselected, mandatory, optional
        button.textContent = participant;
        
        button.onclick = (e) => {
            e.preventDefault();
            cycleParticipantStatus(button, participant);
        };
        
        container.appendChild(button);
    });
}

// ========================
// Toggle Participants
// ========================
function cycleParticipantStatus(button, participant) {
    const currentStatus = button.dataset.status;
    let newStatus;
    
    // Cycle: unselected -> mandatory -> optional -> unselected
    switch (currentStatus) {
        case 'unselected':
            newStatus = 'mandatory';
            break;
        case 'mandatory':
            newStatus = 'optional';
            break;
        case 'optional':
            newStatus = 'unselected';
            break;
        default:
            newStatus = 'unselected';
    }
    
    // עדכן ה-button
    button.dataset.status = newStatus;
    button.className = `participant-button ${newStatus}`;
    
    // עדכן את state - הסר קודם מכל המקומות
    state.mandatoryParticipants = state.mandatoryParticipants.filter(p => p !== participant);
    state.optionalParticipants = state.optionalParticipants.filter(p => p !== participant);
    
    // הוסף לשום מקום המתאים
    if (newStatus === 'mandatory') {
        state.mandatoryParticipants.push(participant);
    } else if (newStatus === 'optional') {
        state.optionalParticipants.push(participant);
    }
    
    updateParticipantCount();
}

function updateParticipantCount() {
    const mandatoryCount = state.mandatoryParticipants.length;
    const optionalCount = state.optionalParticipants.length;
    const totalCount = mandatoryCount + optionalCount;
    
    // הצג סיכום ברור
    if (totalCount === 0) {
        document.querySelector('.selection-summary').textContent = '👥 בחר לפחות משתתף חובה אחד (ניתן להוסיף אופציונליים בלחיצה שנייה)';
    } else {
        document.querySelector('.selection-summary').innerHTML = `
            <p>✓ נבחרו ${totalCount} משתתפים: ${mandatoryCount} חובה + ${optionalCount} אופציונלי</p>
        `;
    }
}

// ========================
// Step Navigation
// ========================
function goToStep(step) {
    // וודא תחילה תנאים
    if (step === 2) {
        if (state.mandatoryParticipants.length === 0) {
            showError('❌ בחר לפחות משתתף אחד חובה');
            return;
        }
    }
    
    // הסתר הכל
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    
    // הצג רק את ה-step המבוקש
    const section = document.getElementById(`step-${step}`);
    if (section) {
        section.classList.add('active');
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

// ========================
// Search Available Slots
// ========================
async function searchAvailableSlots() {
    const targetDateInput = document.getElementById('target-date');
    state.targetDate = targetDateInput.value;
    state.bufferMinutes = parseInt(document.getElementById('buffer-minutes').value);
    const durationMinutes = parseInt(document.getElementById('event-duration-minutes').value);
    
    // וודא בחירה
    if (!state.targetDate) {
        showError('❌ בחר תאריך');
        return;
    }

    if (!durationMinutes || durationMinutes <= 0) {
        showError('❌ הזן משך פגישה תקין בדקות');
        return;
    }

    state.eventDurationHours = durationMinutes / 60;
    
    if (state.mandatoryParticipants.length === 0) {
        showError('❌ בחר לפחות משתתף אחד חובה');
        return;
    }

    // איפוס בחירה קודמת ומעבר למסך בחירת אפשרות זמן
    state.selectedSlot = null;
    const summary = document.getElementById('selected-slot-summary');
    if (summary) {
        summary.classList.add('hidden');
    }

    const confirmBtn = document.getElementById('confirm-btn');
    if (confirmBtn) {
        confirmBtn.disabled = true;
    }

    const suggestedMessage = document.getElementById('suggested-slot-message');
    if (suggestedMessage) {
        suggestedMessage.textContent = '⏳ מחשב אפשרויות זמן...';
    }

    setRescheduleSectionVisible(false);
    clearRescheduleSuggestions();

    const slotsContainer = document.getElementById('slots-container');
    if (slotsContainer) {
        slotsContainer.innerHTML = '';
    }

    goToStep(3);

    try {
        const response = await fetch('/api/available-slots', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                mandatory_participants: state.mandatoryParticipants,
                optional_participants: state.optionalParticipants,
                event_duration_hours: state.eventDurationHours,
                target_date: state.targetDate,
                buffer_minutes: state.bufferMinutes,
            }),
        });

        const data = await response.json();
        if (data.status !== 'success') {
            showError('שגיאה בחיפוש זמנים: ' + data.error);
            return;
        }

        state.availableSlots = data.slots || [];
        renderAvailableSlots(state.availableSlots);

        document.getElementById('confirm-btn').disabled = true;

        if (state.availableSlots.length === 0) {
            if (suggestedMessage) {
                suggestedMessage.textContent = '😞 לא נמצאו זמנים פנויים עבור משך הפגישה שהוזן';
            }
            setRescheduleSectionVisible(true);
            return;
        }

        setRescheduleSectionVisible(false);
        if (suggestedMessage) {
            suggestedMessage.textContent = `✅ נמצאו ${state.availableSlots.length} אפשרויות — בחר אחת כדי לאשר פגישה`;
        }
    } catch (error) {
        console.error('Error searching slots:', error);
        showError('שגיאה בתקשורת עם השרת');
    }
}

function renderAvailableSlots(slots) {
    const container = document.getElementById('slots-container');
    if (!container) {
        return;
    }

    container.innerHTML = '';

    if (!slots || slots.length === 0) {
        container.innerHTML = '<p style="grid-column: 1/-1; text-align: center; padding: 40px;">😞 לא נמצאו זמנים פנויים</p>';
        return;
    }

    slots.forEach((slot, index) => {
        const score = typeof slot.deep_work_score === 'number' ? slot.deep_work_score : 0;
        const deepWorkClass = score > 0.7 ? 'excellent' : score > 0.5 ? 'good' : '';

        const button = document.createElement('button');
        button.type = 'button';
        button.className = `slot-button ${deepWorkClass}`;
        button.innerHTML = `
            <div class="slot-time">${slot.start_time}</div>
            <div class="slot-end-time">עד ${slot.end_time}</div>
            <div class="slot-duration">⏱️ ${slot.duration}</div>
            <div class="deep-work-score ${deepWorkClass}">
                עמוק-עבודה: ${score.toFixed(2)}
            </div>
            <div class="select-indicator">לחץ לבחירה</div>
        `;

        button.onclick = (e) => {
            e.preventDefault();
            selectSlot(index, slot);
        };

        container.appendChild(button);
    });
}

function setRescheduleSectionVisible(isVisible) {
    const section = document.getElementById('reschedule-section');
    if (!section) {
        return;
    }

    if (isVisible) {
        section.classList.remove('hidden');
    } else {
        section.classList.add('hidden');
    }
}

function clearRescheduleSuggestions() {
    state.rescheduleSuggestions = [];
    const container = document.getElementById('reschedule-suggestions');
    if (container) {
        container.innerHTML = '';
    }
}

async function suggestMeetingMoves() {
    const container = document.getElementById('reschedule-suggestions');
    if (!container) {
        return;
    }

    container.innerHTML = '<p class="reschedule-loading">⏳ מחשב הזזות אפשריות...</p>';

    try {
        const response = await fetch('/api/suggest-reschedules', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                mandatory_participants: state.mandatoryParticipants,
                optional_participants: state.optionalParticipants,
                event_duration_hours: state.eventDurationHours,
                target_date: state.targetDate,
                buffer_minutes: state.bufferMinutes,
            }),
        });

        const data = await response.json();
        if (data.status !== 'success') {
            showError('שגיאה בקבלת הצעות הזזה: ' + data.error);
            container.innerHTML = '<p class="reschedule-loading">❌ לא ניתן לחשב הצעות הזזה כרגע</p>';
            return;
        }

        state.rescheduleSuggestions = data.suggestions || [];
        renderRescheduleSuggestions(state.rescheduleSuggestions);
    } catch (error) {
        console.error('Error suggesting reschedules:', error);
        showError('שגיאה בתקשורת עם השרת');
        container.innerHTML = '<p class="reschedule-loading">❌ שגיאת תקשורת</p>';
    }
}

function renderRescheduleSuggestions(suggestions) {
    const container = document.getElementById('reschedule-suggestions');
    if (!container) {
        return;
    }

    container.innerHTML = '';

    if (!suggestions || suggestions.length === 0) {
        container.innerHTML = '<p class="reschedule-loading">לא נמצאו הזזות שיוצרות חלון זמין.</p>';
        return;
    }

    suggestions.forEach((suggestion, index) => {
        const card = document.createElement('div');
        card.className = 'reschedule-card';
        const moves = Array.isArray(suggestion.moves) && suggestion.moves.length > 0
            ? suggestion.moves
            : [{
                participant_name: suggestion.participant_name,
                event_subject: suggestion.event_subject,
                original_time: suggestion.original_time,
                suggested_time: suggestion.suggested_time,
            }];
        const movesHtml = moves.map((move, moveIndex) => `
            <p><strong>הזזה ${moveIndex + 1}:</strong> ${move.event_subject} (${move.participant_name})</p>
            <p><strong>זמן נוכחי:</strong> ${move.original_time}</p>
            <p><strong>זמן חדש:</strong> ${move.suggested_time}</p>
        `).join('');
        card.innerHTML = `
            <p><strong>מספר הזזות נדרש:</strong> ${suggestion.move_count || moves.length}</p>
            ${movesHtml}
            <p><strong>יפתח חלון לפגישה:</strong> ${suggestion.unlocked_meeting_time}</p>
            <p><strong>עמוק-עבודה:</strong> ${suggestion.unlocked_meeting_deep_work_score}</p>
            <button class="btn btn-warning" type="button" onclick="moveMeetingSuggestion(${index})">
                בצע הזזה זו
            </button>
        `;
        container.appendChild(card);
    });
}

async function moveMeetingSuggestion(index) {
    const suggestion = state.rescheduleSuggestions[index];
    if (!suggestion) {
        showError('❌ הצעת ההזזה לא קיימת');
        return;
    }

    const moves = Array.isArray(suggestion.moves) && suggestion.moves.length > 0
        ? suggestion.moves
        : [{
            participant_name: suggestion.participant_name,
            event_subject: suggestion.event_subject,
            original_start_datetime: suggestion.original_start_datetime,
            original_end_datetime: suggestion.original_end_datetime,
            suggested_start_datetime: suggestion.suggested_start_datetime,
            suggested_end_datetime: suggestion.suggested_end_datetime,
        }];

    try {
        for (const move of moves) {
            const response = await fetch('/api/move-meeting', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    participant_name: move.participant_name,
                    event_subject: move.event_subject,
                    from_start_datetime: move.original_start_datetime,
                    from_end_datetime: move.original_end_datetime,
                    to_start_datetime: move.suggested_start_datetime,
                    to_end_datetime: move.suggested_end_datetime,
                }),
            });

            const data = await response.json();
            if (data.status !== 'success') {
                showError('שגיאה בהזזת פגישה: ' + data.error);
                return;
            }
        }

        showToast('✅ הפגישה הוזזה בהצלחה, מחשב מחדש אפשרויות זמן...', 'success');
        await searchAvailableSlots();
    } catch (error) {
        console.error('Error moving meeting:', error);
        showError('שגיאה בתקשורת עם השרת');
    }
}

function selectSlot(index, slot) {
    const allButtons = document.querySelectorAll('.slot-button');
    allButtons.forEach(btn => btn.classList.remove('selected'));

    if (allButtons[index]) {
        allButtons[index].classList.add('selected');
    }

    state.selectedSlot = slot;
    displaySelectedSlotSummary(slot);
    document.getElementById('confirm-btn').disabled = false;

    const suggestedMessage = document.getElementById('suggested-slot-message');
    if (suggestedMessage) {
        suggestedMessage.textContent = `✅ נבחר: ${slot.start_time} - ${slot.end_time}`;
    }
}

function displaySelectedSlotSummary(slot) {
    const summary = document.getElementById('selected-slot-summary');
    summary.classList.remove('hidden');
    
    document.getElementById('summary-date').textContent = state.targetDate;
    document.getElementById('summary-time').textContent = `${slot.start_time} - ${slot.end_time}`;
    document.getElementById('summary-participants').textContent = 
        [...state.mandatoryParticipants, ...state.optionalParticipants].join(', ');
    document.getElementById('summary-deep-work').textContent = (slot.deep_work_score ?? 0).toFixed(2);
}

// ========================
// Book Meeting
// ========================
async function bookMeeting() {
    if (!state.selectedSlot) {
        showError('❌ בחר זמן מהרשימה לפני אישור');
        return;
    }

    const eventSubjectInput = document.getElementById('event-subject');
    const eventSubject = eventSubjectInput?.value?.trim() || 'פגישה';
    
    // הגבר כפתור
    const confirmBtn = document.getElementById('confirm-btn');
    confirmBtn.disabled = true;
    confirmBtn.textContent = '⏳ שומר...';
    
    try {
        const response = await fetch('/api/book-meeting', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                mandatory_participants: state.mandatoryParticipants,
                optional_participants: state.optionalParticipants,
                event_subject: eventSubject,
                start_datetime: state.selectedSlot.start_datetime,
                end_datetime: state.selectedSlot.end_datetime,
            }),
        });
        
        const data = await response.json();
        
        if (data.status === 'success') {
            console.log('✓ הפגישה נוספה בהצלחה');
            showSuccessMessage(data);
            goToStep(4);
        } else {
            showError('שגיאה בהוספת הפגישה: ' + data.error);
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'אשר פגישה ✅';
        }
    } catch (error) {
        console.error('Error booking meeting:', error);
        showError('שגיאה בתקשורת עם השרת');
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'אשר פגישה ✅';
    }
}

// ========================
// Success Message
// ========================
function showSuccessMessage(data) {
    const details = document.getElementById('success-details');
    details.innerHTML = `
        <p><strong>✅ נוספו ${data.created_events} אירועים ל-CSV</strong></p>
        <p>שם הפגישה: ${data.event_subject}</p>
        <p>משתתפים: ${data.participants.join(', ')}</p>
        <p>שעה: ${data.start_time} - ${data.end_time}</p>
        <p style="margin-top: 15px; color: #7f8c8d;">הפגישה נשמרה בקובץ calendar.csv</p>
    `;
}

// ========================
// Upcoming Meetings
// ========================
async function showUpcomingMeetings() {
    goToStep(5);

    const container = document.getElementById('upcoming-meetings-container');
    container.innerHTML = '<p style="text-align:center; padding: 20px;">⏳ טוען פגישות...</p>';

    try {
        const response = await fetch('/api/upcoming-meetings');
        const data = await response.json();

        if (data.status === 'success') {
            renderUpcomingMeetings(data.meetings);
        } else {
            showError('שגיאה בטעינת פגישות: ' + data.error);
            container.innerHTML = '<p style="text-align:center; padding: 20px;">❌ לא ניתן לטעון פגישות</p>';
        }
    } catch (error) {
        console.error('Error loading upcoming meetings:', error);
        showError('שגיאה בתקשורת עם השרת');
        container.innerHTML = '<p style="text-align:center; padding: 20px;">❌ שגיאת תקשורת</p>';
    }
}

function renderUpcomingMeetings(meetings) {
    const container = document.getElementById('upcoming-meetings-container');
    container.innerHTML = '';

    if (!meetings || meetings.length === 0) {
        container.innerHTML = '<p style="text-align:center; padding: 30px;">📭 אין פגישות מהיום ואילך</p>';
        return;
    }

    meetings.forEach((meeting) => {
        const meetingCard = document.createElement('div');
        meetingCard.className = 'meeting-card';
        meetingCard.innerHTML = `
            <h3>${meeting.event_subject}</h3>
            <p><strong>תאריך:</strong> ${meeting.start_date}</p>
            <p><strong>שעה:</strong> ${meeting.start_time} - ${meeting.end_time}</p>
            <p><strong>משתתפים:</strong> ${meeting.participants.join(', ')}</p>
        `;
        container.appendChild(meetingCard);
    });
}

// ========================
// Reset Form
// ========================
function resetForm() {
    state.mandatoryParticipants = [];
    state.optionalParticipants = [];
    state.eventDurationHours = 1;
    state.availableSlots = [];
    state.selectedSlot = null;
    
    // נקה כפתורי משתתפים
    document.querySelectorAll('.participant-button').forEach(btn => {
        btn.dataset.status = 'unselected';
        btn.className = 'participant-button unselected';
    });
    
    // נקה סיכום
    document.getElementById('selected-slot-summary').classList.add('hidden');
    document.getElementById('confirm-btn').disabled = true;
    document.getElementById('confirm-btn').textContent = 'אשר פגישה ✅';
    const durationInput = document.getElementById('event-duration-minutes');
    if (durationInput) {
        durationInput.value = '60';
    }
    const eventSubjectInput = document.getElementById('event-subject');
    if (eventSubjectInput) {
        eventSubjectInput.value = 'פגישה';
    }
    const suggestedMessage = document.getElementById('suggested-slot-message');
    if (suggestedMessage) {
        suggestedMessage.textContent = '⏳ מחשב אפשרויות זמן...';
    }

    setRescheduleSectionVisible(false);
    clearRescheduleSuggestions();
    
    // חזור לשלב 1
    updateParticipantCount();
    goToStep(1);
}

// ========================
// UI Helpers
// ========================
function showError(message) {
    console.error('❌ ' + message);
    showToast(message, 'error');
}

function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.className = `toast`;
    if (type === 'error') {
        toast.style.background = '#e74c3c';
    }
    toast.textContent = message;
    
    document.body.appendChild(toast);
    
    setTimeout(() => {
        toast.style.animation = 'slideUp 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

console.log('📅 Calendar Engine UI Loaded Successfully');
