#!/usr/bin/env node
// Public DOM only. No YouTube private endpoints, browser cookies, or agent CUA.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

export class Stop extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const fail = (status, message) => { throw new Stop(status, message); };
const originSkill = 'youtube-publish';
const channelPattern = /^UC[A-Za-z0-9_-]{22}$/;
const videoPattern = /^[A-Za-z0-9_-]{11}$/;
const labels = {
  create: /^(Create|创建)$/i, upload: /^(Upload videos|上传视频)$/i,
  title: /^(Title(?: \(required\))?|标题(?:（必填）|\(必填\))?|添加一个可描述你视频的标题)/i,
  description: /^(Description|说明|描述|向观看者介绍你的视频)/i,
  more: /^(Show more|显示更多|展开|显示高级设置)$/i, next: /^(Next|下一步|继续)$/i,
  yesKids: /^(Yes,? it'?s made for kids\.?|是，.*面向儿童.*)$/i,
  noKids: /^(No,? it'?s not made for kids\.?|否，.*不是面向儿童.*)$/i,
  thumbnail: /^(Upload thumbnail|上传缩略图|Upload file|上传文件)$/i,
  tags: /^(Tags|标签)$/i,
  notify: /^(Publish to subscriptions feed and notify subscribers|发布到订阅.*并通知订阅者)$/i,
  altered: /^(Altered content|修改的内容|经过改动的内容)$/i,
  yes: /^(Yes|是)$/i, no: /^(No|否)$/i,
  addCaptions: /^(Add subtitles|添加字幕)$/i,
  uploadCaption: /^(Upload file|上传文件)$/i,
  withTiming: /^(With timing|包含时间信息|有时间信息)$/i,
  continue: /^(Continue|继续)$/i,
  done: /^(Done|完成)$/i,
  save: /^(Save|保存)$/i, publish: /^(Publish|发布)$/i,
  public: /^(Public|公开)$/i, private: /^(Private|私享|私人)$/i,
  unlisted: /^(Unlisted|不公开|未列出)$/i,
  schedule: /^(Schedule|定时发布|安排时间)$/i,
  timezone: /^(Time zone|Timezone|时区)$/i,
  utc: /^(\(?(?:GMT|UTC)\+00:00\)?\s*)?(?:Coordinated Universal Time|协调世界时|UTC)$/i,
  date: /^(Date|日期)$/i, time: /^(Time|时间)$/i,
};

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export function validatePayload(p, decision) {
  if (!p || p.version !== 1 || !['publish','schedule'].includes(decision) || p.mode !== decision)
    fail('DECISION_MODE_MISMATCH', '网页上传只接受完全匹配的发布或定时指令；本地草稿不进入网页。');
  const {fingerprint, ...content} = p;
  if (crypto.createHash('sha256').update(canonical(content)).digest('hex') !== fingerprint)
    fail('PAYLOAD_CHANGED', '网页任务内容校验不匹配。');
  if (!p.title || typeof p.title !== 'string' || p.title.length > 100 || typeof p.description !== 'string' || p.description.length > 5000)
    fail('INVALID_METADATA', '标题或正文超限。');
  if (!Array.isArray(p.tags) || p.tags.some(t=>typeof t !== 'string' || !t.trim()) || p.tags.join(',').length > 500)
    fail('INVALID_TAGS', '标签格式或长度不正确。');
  for (const field of ['made_for_kids','contains_synthetic_media','notify_subscribers'])
    if (typeof p[field] !== 'boolean') fail('DECLARATION_REQUIRED', '缺少明确的受众、合成内容或通知声明。');
  if(p.made_for_kids && p.notify_subscribers)
    fail('DECLARATION_CONFLICT','面向儿童的视频不能通知订阅者，请返回看板修改并重新确认。');
  if (!['private','unlisted','public'].includes(p.privacy)) fail('PRIVACY_REQUIRED', '缺少公开范围。');
  if (decision === 'schedule' && (p.privacy !== 'private' || !p.publish_at || !Number.isFinite(Date.parse(p.publish_at)) || Date.parse(p.publish_at) <= Date.now()))
    fail('INVALID_SCHEDULE', '定时必须保持私密直到已确认的未来时刻。');
  if (decision === 'publish' && p.publish_at !== null) fail('INVALID_SCHEDULE', '立即发布不能残留定时时间。');
  if (!p.assets?.video) fail('SOURCE_REQUIRED', '缺少已准备的视频。');
}
export function verifyAssets(p) {
  for (const record of Object.values(p.assets)) {
    if (!record) continue;
    if (!path.isAbsolute(record.path) || fs.lstatSync(record.path).isSymbolicLink() || !fs.statSync(record.path).isFile())
      fail('SOURCE_CHANGED', '准备素材路径无效。');
    const hash = crypto.createHash('sha256');
    const fd = fs.openSync(record.path, 'r');
    try { const buffer=Buffer.alloc(1024*1024); let n; while ((n=fs.readSync(fd,buffer,0,buffer.length,null))) hash.update(buffer.subarray(0,n)); }
    finally { fs.closeSync(fd); }
    if (fs.statSync(record.path).size !== record.size || hash.digest('hex') !== record.sha256)
      fail('SOURCE_CHANGED', '英文视频、封面或字幕已变化，停止上传。');
  }
}
export function utcSchedule(iso) {
  const date = new Date(iso);
  if (!Number.isFinite(date.valueOf()) || date.valueOf() <= Date.now()) fail('SCHEDULE_EXPIRED', '定时时间已失效，不能改成立即发布。');
  if (date.getUTCSeconds() || date.getUTCMilliseconds()) fail('SCHEDULE_PRECISION', '网页排期仅支持分钟，请明确选择到分钟的时间。');
  return {date: date.toISOString().slice(0,10), time: date.toISOString().slice(11,16), instant: date.toISOString()};
}
async function one(locator, what) {
  const visible=[];
  for (let i=0; i<await locator.count(); i++) if(await locator.nth(i).isVisible()) visible.push(locator.nth(i));
  if (visible.length !== 1) fail('ADAPTER_OUTDATED', `无法唯一定位${what}，保留页面，请人工核对。`);
  return visible[0];
}
async function control(scope, role, name, what) { return one(scope.getByRole(role,{name}),what); }
async function choose(scope, name, what) {
  const item=await control(scope,'radio',name,what); await chooseItem(item,what);
}
async function chooseItem(item, what) {
  await item.click();
  const checked=await item.getAttribute('aria-checked');
  if (checked !== 'true' && !(await item.isChecked().catch(()=>false))) fail('SETTING_NOT_APPLIED', `${what}未能确认选中。`);
}
async function fillChecked(field, value, what) {
  await field.fill(value); await field.press('Tab');
  const actual=await field.inputValue().catch(()=>field.innerText());
  if (actual.replace(/\r\n/g,'\n').trimEnd() !== value.replace(/\r\n/g,'\n').trimEnd()) fail('SETTING_NOT_APPLIED', `${what}回读不一致。`);
}
async function fileChooser(page, button, filename) {
  const event=page.waitForEvent('filechooser',{timeout:10000});
  await button.click(); await (await event).setFiles(filename);
}

export class StudioDriver {
  constructor(page, channelId) { this.page=page; this.channelId=channelId; this.videoId=null; }
  async session() {
    const p=this.page;
    await p.waitForFunction(()=>location.hostname==='accounts.google.com' || /\/channel\/UC[\w-]+/.test(location.pathname),null,{timeout:20000}).catch(()=>{});
    const url=new URL(p.url());
    if(url.hostname !== 'studio.youtube.com') fail('NEEDS_LOGIN','请在专用 Chrome 中登录 YouTube Studio。');
    const actual=url.pathname.match(/\/channel\/(UC[A-Za-z0-9_-]{22})/)?.[1];
    if(!actual) fail('NEEDS_LOGIN','Studio 尚未进入频道，请完成登录或频道选择。');
    const text=await p.locator('body').innerText();
    if(/Verify it.?s you|验证是你本人在操作|确认您的身份|unusual traffic|异常流量/i.test(text)) fail('NEEDS_USER','YouTube 要求安全验证，请用户自行完成。');
    if(this.channelId && actual!==this.channelId) fail('CHANNEL_MISMATCH','当前 Chrome 频道不是已配置频道，未上传。');
    return actual;
  }
  async openUpload() {
    const p=this.page;
    if(await p.locator('ytcp-uploads-dialog').isVisible().catch(()=>false)) {
      if(await p.locator('ytcp-uploads-dialog input[type=file][name=Filedata]').count()===1 && await p.getByRole('button',{name:/^(Select files|选择文件)$/i}).isVisible().catch(()=>false)) return;
      fail('EDITOR_ALREADY_ACTIVE','网页存在未完成的上传，不能覆盖或新增上传。');
    }
    await (await control(p,'button',labels.create,'创建按钮')).click();
    await (await control(p,'menuitem',labels.upload,'上传视频菜单')).click();
    await p.getByRole('button',{name:/^(Select files|选择文件)$/i}).waitFor({timeout:15000});
  }
  async upload(p) {
    await this.session(); await this.openUpload();
    // Observed in logged-in Studio on 2026-09-04. Selecting a file STARTS upload.
    const input=this.page.locator('ytcp-uploads-dialog input[type=file][name=Filedata]');
    if(await input.count()!==1) fail('ADAPTER_OUTDATED','上传文件入口发生变化。');
    await input.setInputFiles(p.assets.video.path);
    await this.page.getByRole('textbox',{name:labels.title}).waitFor({timeout:120000});
    this.videoId=await this.findVideoId();
  }
  async findVideoId() {
    const hrefs=await this.page.locator('ytcp-uploads-dialog a[href]').evaluateAll(es=>es.map(e=>e.href));
    const ids=[...new Set(hrefs.map(h=>{
      const u=new URL(h); return u.hostname==='youtu.be'?u.pathname.slice(1):u.hostname.endsWith('youtube.com')?(u.searchParams.get('v')||u.pathname.match(/\/(?:video|shorts)\/([^/]+)/)?.[1]):null;
    }).filter(x=>videoPattern.test(x||'')))];
    return ids.length===1?ids[0]:null;
  }
  async fieldGroup(label) {
    const heading=await one(this.page.getByText(label,{exact:true}),'声明分组');
    for(let depth=1;depth<=4;depth++) {
      const group=heading.locator('xpath='+'..'+('/..'.repeat(depth-1)));
      if(await group.getByRole('radio').count()===2) return group;
    }
    fail('ADAPTER_OUTDATED','无法唯一定位合成内容声明分组。');
  }
  async uploadThumbnail(filename) {
    const imageInput=this.page.locator('ytcp-uploads-dialog input[type=file][accept*="image"]');
    const count=await imageInput.count();
    if(count===1) { await imageInput.setInputFiles(filename); return; }
    if(count>1) fail('ADAPTER_OUTDATED','无法唯一定位缩略图图片入口。');
    await fileChooser(this.page,await control(this.page,'button',labels.thumbnail,'上传缩略图'),filename);
  }
  async dropdown(name, option, what) {
    const box=await control(this.page,'combobox',name,what);
    if(await box.evaluate(e=>e.tagName)==='SELECT') {
      const available=await box.locator('option').evaluateAll(es=>es.map(e=>({label:e.label,value:e.value})));
      const matches=available.filter(item=>option instanceof RegExp?option.test(item.label):item.label===option);
      if(matches.length!==1) fail('ADAPTER_OUTDATED', `${what}没有唯一匹配项。`);
      await box.selectOption(matches[0].value);
      if(await box.inputValue()!==matches[0].value) fail('SETTING_NOT_APPLIED', `${what}未回读到请求值。`);
      return;
    }
    else {
      await box.click();
      const candidate=this.page.getByRole('option',{name:option,exact:true});
      await (await one(candidate,what+'选项')).click();
    }
    const value=await box.inputValue().catch(()=>box.innerText());
    if(!(option instanceof RegExp?value.split('\n').some(line=>option.test(line.trim())):value.includes(option)))
      fail('SETTING_NOT_APPLIED', `${what}未回读到请求值。`);
  }
  async metadata(p,{reuseThumbnail=false}={}) {
    const page=this.page;
    await fillChecked(await control(page,'textbox',labels.title,'标题'),p.title,'标题');
    await fillChecked(await control(page,'textbox',labels.description,'正文'),p.description,'正文');
    const kids=page.locator(`tp-yt-paper-radio-button[name="${p.made_for_kids?'VIDEO_MADE_FOR_KIDS_MFK':'VIDEO_MADE_FOR_KIDS_NOT_MFK'}"]`);
    await chooseItem(await one(kids,'受众声明'),'受众声明');
    if(p.assets.thumbnail && !reuseThumbnail)
      await this.uploadThumbnail(p.assets.thumbnail.path);
    const more=page.getByRole('button',{name:labels.more});
    if(await more.count()===1 && await more.isVisible()) await more.click();
    const altered=page.locator(`tp-yt-paper-radio-button[name="VIDEO_HAS_ALTERED_CONTENT_${p.contains_synthetic_media?'YES':'NO'}"]`);
    await chooseItem(await one(altered,'合成内容声明'),'合成内容声明');
    const notify=await control(page,'checkbox',labels.notify,'订阅通知');
    if(await notify.isChecked()!==p.notify_subscribers) {
      if(!await notify.isEnabled()) fail('DECLARATION_CONFLICT','平台不允许当前受众使用所选通知方式。');
      // Studio nests the visible checkbox container inside the role-bearing
      // control, which intercepts a normal Playwright click.
      await notify.click({force:true});
    }
    if(await notify.isChecked()!==p.notify_subscribers) fail('SETTING_NOT_APPLIED','订阅通知不匹配。');
    const tags=await control(page,'textbox',labels.tags,'标签');
    await tags.fill(p.tags.join(',')); await tags.press('Enter');
    // Chip texts must be read from the tags control's parent; do not silently omit tags.
    const tagText=await tags.locator('..').innerText();
    if(p.tags.some(tag=>!tagText.includes(tag))) fail('SETTING_NOT_APPLIED','标签未能逐项回读。');
    const language=p.default_language==='en-GB'?/^(English \(United Kingdom\)|英语（英国）|英语 \(英国\))$/i:/^(English|英语)$/i;
    const categories={'22':/^(People & Blogs|人物和博客)$/i,'1':/^(Film & Animation|电影和动画)$/i,'10':/^(Music|音乐)$/i,'24':/^(Entertainment|娱乐)$/i,'27':/^(Education|教育)$/i};
    if(!categories[p.category_id]) fail('UNSUPPORTED_CATEGORY','当前网页适配未校准该分类，不能替换分类。');
    // Studio may omit these optional dropdown roles for Shorts. Metadata,
    // tags, declarations and visibility remain authoritative in that view.
    if(await page.getByRole('combobox',{name:/^(Video language|视频语言)$/i}).count())
      await this.dropdown(/^(Video language|视频语言)$/i,language,'视频语言');
    if(await page.getByRole('combobox',{name:/^(Category|类别)$/i}).count())
      await this.dropdown(/^(Category|类别)$/i,categories[p.category_id],'视频分类');
  }
  async captions(p) {
    if(!p.assets.caption) return;
    const page=this.page;
    const add=await control(page,'button',labels.addCaptions,'添加字幕');
    await add.click({timeout:120000});
    const subtitleDialog=await one(page.getByRole('dialog').filter({has:page.getByRole('button',{name:labels.uploadCaption})}),'字幕对话框');
    await (await control(subtitleDialog,'button',labels.uploadCaption,'上传字幕文件')).click();
    const timing=await one(page.getByRole('dialog').filter({has:page.getByRole('radio',{name:labels.withTiming})}),'字幕时间对话框');
    await choose(timing,labels.withTiming,'包含时间信息');
    await fileChooser(page,await control(timing,'button',labels.continue,'继续上传字幕'),p.assets.caption.path);
    await (await control(subtitleDialog,'button',labels.done,'完成字幕')).click();
    const text=await page.locator('ytcp-uploads-dialog').innerText();
    if(!/Subtitles added|字幕已添加|已添加字幕/i.test(text)) fail('CAPTIONS_UNVERIFIED','字幕上传结果未确认，停止最终发布。');
  }
  async advanceToVisibility(p,{skipCaptions=false}={}) {
    const page=this.page;
    await (await control(page,'button',labels.next,'下一步')).click();
    if(!skipCaptions) await this.captions(p);
    for(let i=0;i<5;i++) {
      if(await page.getByRole('radio',{name:labels.public}).isVisible().catch(()=>false)) return;
      const text=await page.locator('ytcp-uploads-dialog').innerText();
      if(/Copyright claim|Copyright strike|版权主张|版权警示|上传失败|Upload failed/i.test(text)) fail('NEEDS_USER','上传或版权检查需要用户查看，停止最终发布。');
      await (await control(page,'button',labels.next,'下一步')).click({timeout:120000});
    }
    fail('ADAPTER_OUTDATED','未进入可见性设置，不能点击提交。');
  }
  async visibility(p) {
    const page=this.page;
    if(p.mode==='schedule') {
      await choose(page,labels.schedule,'定时发布');
      // Explicitly select UTC in Studio; browser timezone alone is not evidence.
      await this.dropdown(labels.timezone,labels.utc,'UTC 时区');
      const zone=await control(page,'combobox',labels.timezone,'时区');
      const zoneText=(await zone.innerText())+' '+(await zone.inputValue().catch(()=>''));
      if(!/UTC|Coordinated Universal Time|协调世界时/i.test(zoneText) || /[+-]0[1-9]:|[+-][1-9]/.test(zoneText)) fail('TIMEZONE_UNVERIFIED','网页时区不是明确的 UTC，停止排期。');
      const target=utcSchedule(p.publish_at);
      const date=await control(page,'textbox',labels.date,'排期日期');
      const time=await control(page,'textbox',labels.time,'排期时间');
      // Use ISO inputs only if the UI accepts them verbatim. Never assume a date locale.
      await fillChecked(date,target.date,'UTC 日期');
      await fillChecked(time,target.time,'UTC 时间');
      this.requestedUtc=target.instant;
    } else await choose(page,labels[p.privacy],'公开范围');
    const premiere=page.getByRole('checkbox',{name:/Premiere|首映/i});
    if(await premiere.count()===1 && await premiere.isVisible() && await premiere.isChecked())
      fail('UNREQUESTED_PREMIERE','网页默认选中了首映，但本次未授权首映。请用户核对。');
  }
  async submit(p) {
    if(p.mode==='schedule') utcSchedule(p.publish_at);
    this.videoId=this.videoId||await this.findVideoId();
    if(!this.videoId) fail('VIDEO_ID_UNVERIFIED','上传尚未返回可核对的视频编号，不能最终提交。');
    const name=p.mode==='schedule'?labels.schedule:(p.privacy==='public'?labels.publish:labels.save);
    await (await control(this.page,'button',name,'最终提交按钮')).click({timeout:120000});
    // One click only. Studio keeps its upload dialog in the DOM behind its
    // post-publication share dialog, so that dialog's visible confirmation is
    // the authoritative public-DOM receipt for an immediate public release.
    const share=this.page.locator('ytcp-video-share-dialog');
    await share.waitFor({state:'visible',timeout:30000}).catch(()=>{});
    const text=await this.page.locator('body').innerText();
    const shareText=await share.innerText().catch(()=> '');
    const publishedReceipt=await share.isVisible().catch(()=>false) && /Video published|Published on|视频发布时间|发布于/i.test(shareText);
    const confirmed=p.mode==='schedule'?/Video scheduled|视频已定时发布|视频已安排|已安排发布时间/i.test(text):publishedReceipt||/Video published|视频已发布|Video saved|视频已保存/i.test(text);
    return {status:confirmed?(p.mode==='schedule'?'SCHEDULE_SUBMITTED':'SUBMITTED'):'SUBMISSION_UNVERIFIED',
            outcome:confirmed?'pending':'unknown',video_id:this.videoId,url:`https://youtu.be/${this.videoId}`,
            requested_publish_at:p.publish_at,youtube_contacted:true,
            message:confirmed?'Studio 已确认提交；需核对频道内容页的最终状态，不自动再次上传。':'已点击提交但结果未确认，请查看当前 Chrome，禁止重复提交。'};
  }
}

export async function execute(driver, payload, options, journal) {
  validatePayload(payload,options.decision);
  if(!options.commit) fail('NEEDS_AUTHORIZATION','选择文件会立即上传，必须先有确切许可和 --commit。');
  if(!channelPattern.test(options.channelId||'')) fail('CHANNEL_REQUIRED','必须绑定用户确认的具体频道。');
  verifyAssets(payload);
  await driver.session();
  journal.claim({fingerprint:payload.fingerprint,decision:payload.mode,channelId:options.channelId,jobId:payload.job_id});
  let uploadStarted=false;
  try {
    journal.mark('UPLOAD_STARTED'); uploadStarted=true;
    await driver.upload(payload);
    journal.mark('UPLOADED',{videoId:driver.videoId});
    await driver.metadata(payload); await driver.advanceToVisibility(payload); await driver.visibility(payload);
    verifyAssets(payload); validatePayload(payload,options.decision);
    journal.mark('SUBMITTING',{videoId:driver.videoId});
    const result=await driver.submit(payload);
    if(payload.auto_tags) {
      result.warnings=[...(result.warnings||[]),'网页渠道未调用 API 热度检索，保留原有英化相关标签；本次未核验实时热度。'];
      result.message=(result.message||'')+' 已保留原有相关标签，未核验实时热度。';
    }
    journal.mark('FINISHED',{result}); return result;
  } catch(error) {
    const result={status:error.status||'BROWSER_INTERRUPTED',outcome:uploadStarted?'partial':'needs_user',
                  video_id:driver.videoId||null,youtube_contacted:uploadStarted,
                  message:uploadStarted?'网页上传可能已经开始，后续步骤未完成。请保留当前页面并核对，不要重新上传。':error.message};
    journal.mark('PAUSED',{result,stopReason:error.status||'BROWSER_INTERRUPTED'}); return result;
  }
}
export async function resume(driver, payload, options, journal) {
  validatePayload(payload,options.decision);
  if(!options.commit) fail('NEEDS_AUTHORIZATION','继续现有上传仍需要明确的提交许可。');
  if(!channelPattern.test(options.channelId||'')) fail('CHANNEL_REQUIRED','必须绑定用户确认的具体频道。');
  verifyAssets(payload);
  await driver.session();
  if(!await driver.page.locator('ytcp-uploads-dialog').count())
    fail('EDITOR_NOT_ACTIVE','未找到已有上传编辑页，不能重新选择文件。');
  driver.videoId=await driver.findVideoId();
  if(!driver.videoId) fail('VIDEO_ID_UNVERIFIED','已有上传尚未提供可核对的视频编号，不能继续。');
  journal.mark('RESUMING',{videoId:driver.videoId});
  try {
    await driver.metadata(payload,{reuseThumbnail:true}); journal.mark('METADATA_APPLIED',{videoId:driver.videoId});
    await driver.advanceToVisibility(payload,{skipCaptions:true}); await driver.visibility(payload);
    verifyAssets(payload); validatePayload(payload,options.decision);
    journal.mark('SUBMITTING',{videoId:driver.videoId});
    const result=await driver.submit(payload);
    if(payload.auto_tags) {
      result.warnings=[...(result.warnings||[]),'网页渠道未调用 API 热度检索，保留原有英化相关标签；本次未核验实时热度。'];
      result.message=(result.message||'')+' 已保留原有相关标签，未核验实时热度。';
    }
    journal.mark('FINISHED',{result}); return result;
  } catch(error) {
    const result={status:error.status||'BROWSER_INTERRUPTED',outcome:'partial',video_id:driver.videoId,
                  youtube_contacted:true,message:`已有上传的后续设置未完成：${error.message}。请保留当前页面并核对，不会重新上传。`};
    journal.mark('PAUSED',{result,stopReason:error.status||'BROWSER_INTERRUPTED'}); return result;
  }
}
export function journalAt(directory) {
  if(!path.isAbsolute(directory)) fail('STATE_REQUIRED','记录目录必须是绝对路径。');
  const file=path.join(directory,'youtube-browser-attempt.json'); let data;
  return {
    claim(value) {
      data={...value,stage:'CLAIMED',startedAt:new Date().toISOString()};
      try {fs.writeFileSync(file,JSON.stringify(data,null,2),{flag:'wx'});}
      catch(e) { if(e.code==='EEXIST') fail('JOB_ALREADY_ATTEMPTED','此任务已有网页执行记录，不能重复上传。'); throw e; }
    },
    mark(stage,extra={}) {data={...data,...extra,stage,updatedAt:new Date().toISOString()}; const temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);}
  };
}
export function pausedJournalAt(directory,payload,channelId) {
  if(!path.isAbsolute(directory)) fail('STATE_REQUIRED','记录目录必须是绝对路径。');
  const file=path.join(directory,'youtube-browser-attempt.json'); let data;
  try { data=JSON.parse(fs.readFileSync(file,'utf8')); }
  catch { fail('RECOVERY_RECORD_MISSING','找不到已有上传的恢复记录，不能重新选择文件。'); }
  if(data.stage!=='PAUSED' || data.fingerprint!==payload.fingerprint || data.channelId!==channelId || data.jobId!==payload.job_id)
    fail('RECOVERY_RECORD_MISMATCH','已有上传记录与当前已确认任务不一致，不能继续。');
  return {mark(stage,extra={}) {data={...data,...extra,stage,updatedAt:new Date().toISOString()}; const temp=file+'.tmp';fs.writeFileSync(temp,JSON.stringify(data,null,2));fs.renameSync(temp,file);}};
}
async function activeEditorPage(browser) {
  for(let attempt=0;attempt<20;attempt++) {
    for(const context of browser.contexts()) for(const candidate of context.pages()) {
      if(!candidate.url().startsWith('https://studio.youtube.com/')) continue;
      const dialog=candidate.locator('ytcp-uploads-dialog');
      if(await dialog.count()) return candidate;
    }
    await new Promise(resolve=>setTimeout(resolve,500));
  }
  return null;
}
function parse(argv) {
  const command=argv.shift(); const options={};
  while(argv.length) { const key=argv.shift(); if(!key.startsWith('--')) fail('INVALID_ARGUMENT','无效参数'); options[key.slice(2)]=['--commit','--inspect-editor'].includes(key)?true:argv.shift(); }
  return {command,options};
}
async function main() {
  const {command,options}=parse(process.argv.slice(2));
  if(!['inspect','preflight','dispatch','resume','verify'].includes(command)) fail('INVALID_COMMAND','未知网页操作。');
  if(!/^http:\/\/(127\.0\.0\.1|localhost):\d{1,5}$/.test(options['cdp-url']||'')) fail('CDP_REQUIRED','只允许本机 Chrome 调试端口。');
  let payload;
  if(command==='dispatch'||command==='resume') {
    payload=JSON.parse(fs.readFileSync(0,'utf8')); validatePayload(payload,options.decision);
    if(!options.commit || !channelPattern.test(options['channel-id']||'')) fail('NEEDS_AUTHORIZATION','需要明确的提交许可与频道绑定。');
    verifyAssets(payload);
  }
  const {chromium}=await import('playwright');
  const browser=await chromium.connectOverCDP(options['cdp-url'],{isLocal:true,noDefaults:true,timeout:10000});
  const context=browser.contexts()[0];
  if(!context) fail('BROWSER_UNAVAILABLE','Chrome 未提供可用上下文。');
  const pages=context.pages().filter(p=>p.url().startsWith('https://studio.youtube.com/'));
  let page;
  if(command==='dispatch') { page=await context.newPage(); await page.goto(`https://studio.youtube.com/channel/${options['channel-id']}`,{waitUntil:'domcontentloaded',timeout:30000}); }
  else if(command==='resume') {
    page=await activeEditorPage(browser);
    if(!page) fail('EDITOR_NOT_ACTIVE','未找到已有上传编辑页，不能重新选择文件。');
  }
  else { page=pages[0]||await context.newPage(); if(!pages.length) await page.goto('https://studio.youtube.com/',{waitUntil:'domcontentloaded',timeout:30000}); }
  const driver=new StudioDriver(page,options['channel-id']);
  const channelId=await driver.session();
  if(command==='dispatch') return execute(driver,payload,{decision:options.decision,commit:options.commit,channelId:options['channel-id']},journalAt(options['state-dir']));
  if(command==='resume') return resume(driver,payload,{decision:options.decision,commit:options.commit,channelId:options['channel-id']},pausedJournalAt(options['state-dir'],payload,options['channel-id']));
  if(command==='verify') {
    if(!videoPattern.test(options['video-id']||'')) fail('VIDEO_ID_REQUIRED','核验需要已保存的视频编号。');
    // Read-only navigation; never click save or mutate an existing item here.
    await page.goto(`https://studio.youtube.com/video/${options['video-id']}/edit`,{waitUntil:'domcontentloaded',timeout:30000});
    return {status:'MANUAL_RESULT_REVIEW',outcome:'needs_user',video_id:options['video-id'],message:'已打开原视频详情供核对，没有重新上传或保存修改。'};
  }
  if(options['inspect-editor']) await driver.openUpload();
  return {status:'BROWSER_PREFLIGHT_OK',event:'browser_preflight_ok',outcome:'success',channel_id:channelId,
          uploadEntryObserved:await page.locator('ytcp-uploads-dialog input[type=file][name=Filedata]').count()===1,
          youtube_contacted:true,uploaded:false,transport:'browser',message:'专用 Chrome 已连接频道；未上传任何文件。'};
}
if(import.meta.url===pathToFileURL(process.argv[1]||'').href) {
  main().then(result=>process.stdout.write(JSON.stringify({...result,originSkill})+'\n',()=>process.exit(0)),
    error=>process.stdout.write(JSON.stringify({originSkill,status:error.status||'BROWSER_UNAVAILABLE',outcome:'needs_user',message:error.status?error.message:'网页连接或页面结构不可用，请保留 Chrome 并检查；未自动重试。'})+'\n',()=>process.exit(0)));
}
