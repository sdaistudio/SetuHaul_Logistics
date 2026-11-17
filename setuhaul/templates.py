"""Reply catalogue. Deterministic, reviewed text in English, Hindi and Hinglish.

The service never free-generates driver-facing text. A reply is a template key
plus slot values that come from tool results; the output guardrail checks the
rendered text against those values before it is sent.
"""
from __future__ import annotations

T = {
    "ask_shipment": {
        "en": "You have {n} trips today:\n{trips}\nWhich one is this about? Reply 1 or 2.",
        "hi": "आज आपकी {n} ट्रिप हैं:\n{trips}\nयह किस ट्रिप के बारे में है? 1 या 2 लिखें।",
        "hinglish": "Aaj aapki {n} trip hain:\n{trips}\nYeh kis trip ke baare mein hai? 1 ya 2 likhein.",
    },
    "ask_eta": {
        "en": "Thanks. What time do you expect to reach {facility}? For example: 11:30.",
        "hi": "धन्यवाद। आप {facility} कितने बजे तक पहुँचेंगे? जैसे: 11:30",
        "hinglish": "Thanks. Aap {facility} kitne baje tak pahunchoge? Jaise: 11:30",
    },
    "ask_eta_vague": {
        "en": "To find a dock I need a clock time. Roughly what time will you reach {facility}? A range like 12:30-13:00 is fine.",
        "hi": "डॉक ढूँढने के लिए समय चाहिए। आप लगभग कितने बजे {facility} पहुँचेंगे? 12:30-13:00 जैसा अंदाज़ा भी चलेगा।",
        "hinglish": "Dock dhoondhne ke liye time chahiye. Aap lagbhag kitne baje {facility} pahunchoge? 12:30-13:00 jaisa range bhi chalega.",
    },
    "current_ok": {
        "en": "Your {window} slot on dock {dock} ({status}) still works with arrival at {eta}. Expected dock start about {start}. No change needed.",
        "hi": "{eta} पहुँचने पर भी आपका डॉक {dock} {window} स्लॉट ({status}) ठीक है। डॉक लगभग {start} बजे मिलेगा। कोई बदलाव नहीं।",
        "hinglish": "{eta} pahunchne par bhi aapka dock {dock} {window} slot ({status}) theek hai. Dock lagbhag {start} baje milega. Koi change nahi.",
    },
    "options_head": {
        "en": "I am planning for arrival at {eta}{assumption}. Slots you can reach at {facility}:",
        "hi": "मैं {eta} पहुँचने के हिसाब से देख रहा हूँ{assumption}। {facility} पर ये स्लॉट मिल सकते हैं:",
        "hinglish": "Main {eta} pahunchne ke hisaab se dekh raha hoon{assumption}. {facility} par ye slot mil sakte hain:",
    },
    "option_line": {
        "en": "{n}) Dock {dock}: start about {start}, unloading done about {end}{flag}",
        "hi": "{n}) डॉक {dock}: लगभग {start} शुरू, {end} तक अनलोड{flag}",
        "hinglish": "{n}) Dock {dock}: lagbhag {start} start, {end} tak unload{flag}",
    },
    "options_tail": {
        "en": "Reply with the number to book. Nothing is booked until you choose.",
        "hi": "बुक करने के लिए नंबर लिखें। आपके चुनने तक कुछ बुक नहीं होगा।",
        "hinglish": "Book karne ke liye number likhein. Aapke chunne tak kuch book nahi hoga.",
    },
    "flag_approval": {"en": " (needs warehouse approval)", "hi": " (वेयरहाउस की मंज़ूरी ज़रूरी)", "hinglish": " (warehouse approval zaroori)"},
    "assume_delay": {
        "en": " (your planned time {base} + {delay} min)", "hi": " (आपका तय समय {base} + {delay} मिनट)",
        "hinglish": " (aapka planned time {base} + {delay} min)",
    },
    "assume_low": {
        "en": " (planning for the late end, {planning}, so the slot is not missed)",
        "hi": " (देर वाले समय {planning} के हिसाब से, ताकि स्लॉट न छूटे)",
        "hinglish": " (late wale time {planning} ke hisaab se, taaki slot na chhoote)",
    },
    "assume_gateout": {
        "en": "I have read your time limit as: you must leave the gate by {t}.",
        "hi": "मैंने समझा है कि आपको {t} तक गेट से निकलना है।",
        "hinglish": "Maine samjha hai ki aapko {t} tak gate se nikalna hai.",
    },
    "option_gone": {
        "en": "Sorry, dock {dock} at {start} was just taken by another truck.",
        "hi": "माफ़ कीजिए, डॉक {dock} {start} वाला स्लॉट अभी दूसरे ट्रक को मिल गया।",
        "hinglish": "Sorry, dock {dock} {start} wala slot abhi doosre truck ko mil gaya.",
    },
    "booked": {
        "en": "Booking confirmed: dock {dock}, start about {start}. {released}Please be at the gate by {report_by}.",
        "hi": "बुकिंग पक्की: डॉक {dock}, लगभग {start} बजे। {released}कृपया {report_by} तक गेट पर पहुँचें।",
        "hinglish": "Booking confirmed: dock {dock}, lagbhag {start} baje. {released}Please {report_by} tak gate par pahunchein.",
    },
    "booked_yard": {
        "en": "Booking confirmed: dock {dock}, start about {start}. {released}You are already inside: wait in the yard for the call to the dock.",
        "hi": "बुकिंग पक्की: डॉक {dock}, लगभग {start} बजे। {released}आप अंदर हैं: डॉक के बुलावे तक यार्ड में रुकें।",
        "hinglish": "Booking confirmed: dock {dock}, lagbhag {start} baje. {released}Aap andar ho: dock ke call tak yard mein rukiye.",
    },
    "released_old": {"en": "Your old {old} slot is released. ", "hi": "आपका पुराना {old} स्लॉट छोड़ दिया गया है। ",
                     "hinglish": "Aapka purana {old} slot chhod diya gaya hai. "},
    "pending": {
        "en": "I have requested dock {dock} at about {start} for you. It is NOT confirmed yet: the warehouse coordinator must approve it ({why}). I will message you within {ttl} minutes.",
        "hi": "आपके लिए डॉक {dock} लगभग {start} बजे का अनुरोध भेजा है। यह अभी पक्का नहीं है: वेयरहाउस कोऑर्डिनेटर की मंज़ूरी चाहिए ({why})। {ttl} मिनट में बताऊँगा।",
        "hinglish": "Aapke liye dock {dock} lagbhag {start} baje ki request bheji hai. Yeh abhi confirm nahi hai: warehouse coordinator ki approval chahiye ({why}). {ttl} minute mein bataunga.",
    },
    "no_slot": {
        "en": "I cannot find a valid {dock_type} dock slot for arrival at {eta} today: {why}. I have passed your case to the {role} with all details. Please do not go to the dock until they contact you.",
        "hi": "{eta} पहुँचने पर आज कोई सही {dock_type} डॉक स्लॉट नहीं मिला: {why}। आपका मामला सभी जानकारी के साथ {role} को भेज दिया है। उनके संपर्क करने तक डॉक पर न जाएँ।",
        "hinglish": "{eta} pahunchne par aaj koi sahi {dock_type} dock slot nahi mila: {why}. Aapka case poori details ke saath {role} ko bhej diya hai. Unke contact karne tak dock par mat jaiye.",
    },
    "safety": {
        "en": "Your safety comes first. Stop somewhere safe. If anyone is hurt, call 112 now. I have alerted the operations team and a person will call you.",
        "hi": "आपकी सुरक्षा सबसे पहले है। किसी सुरक्षित जगह रुकें। कोई घायल है तो अभी 112 पर कॉल करें। ऑपरेशंस टीम को बता दिया है, कोई आपको कॉल करेगा।",
        "hinglish": "Aapki safety sabse pehle hai. Kisi safe jagah ruk jaiye. Koi ghayal hai to abhi 112 call karein. Operations team ko bata diya hai, koi aapko call karega.",
    },
    "human": {
        "en": "I have asked the {role} to contact you. Your trip details and this chat have been shared with them.",
        "hi": "मैंने {role} को आपसे संपर्क करने को कहा है। आपकी ट्रिप और यह चैट उनके साथ साझा कर दी है।",
        "hinglish": "Maine {role} ko aapse contact karne ko bola hai. Aapki trip details aur yeh chat unke saath share kar di hai.",
    },
    "early_none": {
        "en": "You are early. Arriving early does not move you ahead of trucks already scheduled. No dock is free before your {window} slot, so please wait in the yard. We will call you to the dock.",
        "hi": "आप जल्दी पहुँचे हैं। जल्दी आने से पहले से तय ट्रकों से आगे नहीं जा सकते। आपके {window} स्लॉट से पहले कोई डॉक खाली नहीं है, कृपया यार्ड में रुकें। डॉक के लिए बुलाया जाएगा।",
        "hinglish": "Aap jaldi pahunche ho. Jaldi aane se pehle se scheduled trucks se aage nahi ja sakte. Aapke {window} slot se pehle koi dock khaali nahi hai, yard mein ruk jaiye. Dock ke liye bulaya jayega.",
    },
    "early_offer_head": {
        "en": "You are early. Arriving early does not move you ahead of scheduled trucks, but this free capacity does not affect anyone:",
        "hi": "आप जल्दी पहुँचे हैं। जल्दी आने से आप तय ट्रकों से आगे नहीं जाते, पर यह खाली जगह किसी को प्रभावित नहीं करती:",
        "hinglish": "Aap jaldi pahunche ho. Jaldi aane se aap scheduled trucks se aage nahi jaate, par yeh khaali jagah kisi ko affect nahi karti:",
    },
    "status": {
        "en": "Your current booking: dock {dock}, {window} ({status}).{queue}",
        "hi": "आपकी मौजूदा बुकिंग: डॉक {dock}, {window} ({status})।{queue}",
        "hinglish": "Aapki current booking: dock {dock}, {window} ({status}).{queue}",
    },
    "status_queue": {"en": " You are number {pos} in the yard queue.", "hi": " यार्ड कतार में आपका नंबर {pos} है।",
                     "hinglish": " Yard queue mein aapka number {pos} hai."},
    "status_none": {
        "en": "You have no active dock booking right now. Tell me your arrival time and I will check options.",
        "hi": "अभी आपकी कोई डॉक बुकिंग नहीं है। पहुँचने का समय बताएँ, मैं विकल्प देखता हूँ।",
        "hinglish": "Abhi aapki koi dock booking nahi hai. Pahunchne ka time batao, main options dekhta hoon.",
    },
    "cancelled_shipment": {
        "en": "This shipment and its appointment were cancelled. Please contact dispatch before travelling.",
        "hi": "यह शिपमेंट और इसकी बुकिंग रद्द हो चुकी है। निकलने से पहले डिस्पैच से बात करें।",
        "hinglish": "Yeh shipment aur iski booking cancel ho chuki hai. Nikalne se pehle dispatch se baat karein.",
    },
    "declined": {
        "en": "Okay, nothing is booked. Send a new arrival time, or write 'manager' to talk to a coordinator.",
        "hi": "ठीक है, कुछ बुक नहीं किया। नया समय भेजें, या कोऑर्डिनेटर से बात के लिए 'मैनेजर' लिखें।",
        "hinglish": "Theek hai, kuch book nahi kiya. Naya time bhejo, ya coordinator se baat ke liye 'manager' likho.",
    },
    "unknown": {
        "en": "Sorry, I did not understand. You can write, for example: 'late by 1 hour', 'reaching 11:30' or 'option 2'.",
        "hi": "माफ़ कीजिए, समझ नहीं आया। आप ऐसे लिख सकते हैं: '1 घंटा लेट', '11:30 तक पहुँचूँगा' या 'विकल्प 2'।",
        "hinglish": "Sorry, samajh nahi aaya. Aise likh sakte ho: '1 ghanta late', '11:30 tak pahunch jaunga' ya 'option 2'.",
    },
    "priority_note": {
        "en": "Dock order follows the priority on the shipment record; I cannot change it from chat.",
        "hi": "डॉक का क्रम शिपमेंट रिकॉर्ड की प्राथमिकता से तय होता है; चैट से इसे नहीं बदला जा सकता।",
        "hinglish": "Dock ka order shipment record ki priority se tay hota hai; chat se ise change nahi kar sakte.",
    },
    "pick_valid": {
        "en": "Please reply with one of the option numbers: {nums}.",
        "hi": "कृपया इनमें से कोई नंबर लिखें: {nums}।",
        "hinglish": "Please inme se koi number likho: {nums}.",
    },
}

ROLE_NAMES = {
    "WAREHOUSE_COORDINATOR": {"en": "warehouse coordinator", "hi": "वेयरहाउस कोऑर्डिनेटर", "hinglish": "warehouse coordinator"},
    "SHIFT_MANAGER": {"en": "shift manager", "hi": "शिफ्ट मैनेजर", "hinglish": "shift manager"},
    "NIGHT_SHIFT_COORDINATOR": {"en": "night-shift coordinator", "hi": "नाइट शिफ्ट कोऑर्डिनेटर", "hinglish": "night shift coordinator"},
    "GATE_SUPERVISOR": {"en": "gate supervisor", "hi": "गेट सुपरवाइज़र", "hinglish": "gate supervisor"},
    "OPERATIONS": {"en": "operations team", "hi": "ऑपरेशंस टीम", "hinglish": "operations team"},
}

WHY = {
    "SLOT_BLOCKED": {"en": "the only compatible dock is blocked", "hi": "सही डॉक बंद है", "hinglish": "sahi dock band hai"},
    "DOCK_EVENT": {"en": "the compatible dock is under maintenance or broken", "hi": "सही डॉक मेंटेनेंस/खराबी में है",
                   "hinglish": "sahi dock maintenance/kharabi mein hai"},
    "DOCK_BUSY": {"en": "all compatible docks are already booked", "hi": "सभी सही डॉक पहले से बुक हैं",
                  "hinglish": "saare sahi dock pehle se book hain"},
    "ETA_AFTER_WINDOW": {"en": "the remaining windows end before you arrive", "hi": "बाकी स्लॉट आपके पहुँचने से पहले खत्म हो जाते हैं",
                         "hinglish": "baaki slot aapke pahunchne se pehle khatam ho jaate hain"},
    "AFTER_CLOSE": {"en": "unloading would run past closing time", "hi": "अनलोडिंग बंद होने के समय के बाद तक चलेगी",
                    "hinglish": "unloading closing time ke baad tak chalegi"},
    "DRIVER_DEADLINE": {"en": "no slot finishes before your time limit", "hi": "आपकी समय-सीमा से पहले कोई स्लॉट खत्म नहीं होता",
                        "hinglish": "aapki time limit se pehle koi slot khatam nahi hota"},
    "BEFORE_DRIVER_EARLIEST": {"en": "nothing is free after the time you asked for", "hi": "आपके बताए समय के बाद कुछ खाली नहीं",
                               "hinglish": "aapke bataye time ke baad kuch khaali nahi"},
    "WEIGHT": {"en": "no dock can take this weight", "hi": "कोई डॉक इतना वज़न नहीं ले सकता", "hinglish": "koi dock itna weight nahi le sakta"},
    "DEFAULT": {"en": "no compatible capacity is left", "hi": "कोई सही जगह खाली नहीं है", "hinglish": "koi sahi jagah khaali nahi hai"},
}

APPROVAL_WHY = {
    "LATE_START_RULE": {"en": "start after 21:00", "hi": "21:00 के बाद शुरुआत", "hinglish": "21:00 ke baad start"},
    "LOW_CONFIDENCE_ETA": {"en": "arrival time is uncertain", "hi": "पहुँचने का समय पक्का नहीं", "hinglish": "pahunchne ka time pakka nahi"},
    "DATA_CONFLICT": {"en": "shipment data needs a check", "hi": "शिपमेंट डेटा जाँचना है", "hinglish": "shipment data check karna hai"},
}

DOCK_TYPE_NAME = {
    "STANDARD": {"en": "standard", "hi": "सामान्य", "hinglish": "standard"},
    "REEFER": {"en": "refrigerated", "hi": "रेफ्रिजरेटेड", "hinglish": "reefer"},
    "HEAVY": {"en": "heavy-vehicle", "hi": "हेवी व्हीकल", "hinglish": "heavy"},
}

STATUS_NAME = {
    "CONFIRMED": {"en": "confirmed", "hi": "पक्की", "hinglish": "confirmed"},
    "PENDING_CONFIRMATION": {"en": "waiting for warehouse approval", "hi": "वेयरहाउस मंज़ूरी बाकी", "hinglish": "warehouse approval baaki"},
    "IN_PROGRESS": {"en": "unloading", "hi": "अनलोडिंग जारी", "hinglish": "unloading chal rahi"},
}


def render(key: str, lang: str, **kw) -> str:
    lang = lang if lang in ("en", "hi", "hinglish") else "en"
    return T[key][lang].format(**kw)


def word(table: dict, key: str, lang: str) -> str:
    lang = lang if lang in ("en", "hi", "hinglish") else "en"
    return table.get(key, table.get("DEFAULT", {})).get(lang, key) if key in table or "DEFAULT" in table else key
