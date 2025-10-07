const chatbox=document.getElementById("chatbox");
const messageInput=document.getElementById("message");
const sendBtn=document.getElementById("send");
function addMessage(sender,text){const msg=document.createElement("div");msg.innerHTML=`<b>${sender}:</b> ${text}`;chatbox.appendChild(msg);chatbox.scrollTop=chatbox.scrollHeight}
async function sendMessage(){const message=messageInput.value.trim();if(!message)return;addMessage("Você",message);messageInput.value="";const response=await fetch("http://127.0.0.1:5000/message",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message,user_id:"localuser"})});const data=await response.json();addMessage("Blarry AI",data.reply)}
sendBtn.addEventListener("click",sendMessage);messageInput.addEventListener("keypress",e=>{if(e.key==="Enter")sendMessage()})
