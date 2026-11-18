'use strict';
// Показ символов и проверка подтверждения рядом с полем ввода.
(()=>{
 const byId=id=>document.getElementById(id);
 const buttons=[...document.querySelectorAll('[data-password-for]')];
 function visibility(button,visible){
  const input=byId(button.dataset.passwordFor);
  input.type=visible?'text':'password';
  button.setAttribute('aria-pressed',String(visible));
  button.setAttribute('aria-label',visible?'Скрыть пароль':'Показать пароль');
  button.title=visible?'Скрыть пароль':'Показать пароль';
  button.querySelector('[data-icon="show"]').toggleAttribute('hidden',visible);
  button.querySelector('[data-icon="hide"]').toggleAttribute('hidden',!visible);
 }
 function validate(){
  const repeat=byId('repeatPassword');
  const mismatch=repeat.value.length>0&&byId('newPassword').value!==repeat.value;
  repeat.setCustomValidity(mismatch?'Пароли не совпадают. Повторите новый пароль.':'');
  repeat.setAttribute('aria-invalid',String(mismatch));
  byId('passwordMismatch').hidden=!mismatch;
  return !mismatch;
 }
 function reset(){
  buttons.forEach(button=>visibility(button,false));
  byId('repeatPassword').setCustomValidity('');
  byId('repeatPassword').setAttribute('aria-invalid','false');
  byId('passwordMismatch').hidden=true;
 }
 for(const button of buttons){
  button.addEventListener('click',()=>{
   const input=byId(button.dataset.passwordFor);
   visibility(button,input.type==='password');
   input.focus({preventScroll:true});
  });
 }
 for(const id of ['newPassword','repeatPassword']){
  byId(id).addEventListener('input',validate);
  byId(id).addEventListener('blur',validate);
 }
 byId('passwordForm').addEventListener('invalid',validate,true);
 byId('passwordForm').addEventListener('reset',reset);
 byId('loginForm').addEventListener('reset',reset);
 window.AtlasPasswords={validate,reset};
})();
