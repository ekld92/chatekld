// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest';

beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '<aside id="activity-bar"><span id="activity-label-text"></span><div id="activity-tasks"></div><div id="activity-events"></div></aside>';
});
it('keeps failures visible and never infers success from stream end', async () => {
    const ui = await import('../../static/js/ui.js');
    ui.taskBegin('one', 'Preparing');
    ui.taskUpdate('one', {message:'Provider failed', state:'failed'});
    ui.taskEnd('one');
    expect(document.getElementById('activity-tasks').textContent).toContain('Needs attention');
    expect(document.getElementById('activity-label-text').textContent).toContain('1 need attention');
    ui.taskBegin('two', 'Compiling'); ui.taskEnd('two');
    expect(document.getElementById('activity-tasks').textContent).toContain('Ended — see result');
    expect(document.getElementById('activity-label-text').textContent).not.toContain('completed this session');
});
it('shows bounded progress and counts a confirmed completion once', async () => {
    const ui = await import('../../static/js/ui.js');
    ui.taskBegin('one', 'Extracting'); ui.taskUpdate('one', {completed:3,total:5,message:'Three documents examined'});
    expect(document.querySelector('progress').value).toBe(3);
    ui.taskEnd('one','completed'); ui.taskEnd('one','completed');
    expect(document.getElementById('activity-label-text').textContent).toContain('1 completed this session');
    for (let n=0;n<200;n++) { ui.taskBegin('two','Working'); ui.taskUpdate('two',{message:`event ${n}`}); }
    expect(document.getElementById('activity-events').children).toHaveLength(150);
});

it('clears a previous run outcome and counts when restarting the same task', async () => {
    const ui = await import('../../static/js/ui.js');
    ui.taskBegin('scan', 'Audit scanning');
    ui.taskUpdate('scan', {completed: 5, total: 5, message: 'Previous scan finished'});
    ui.taskEnd('scan', 'completed');
    ui.taskBegin('scan', 'Audit scanning');
    const host = document.getElementById('activity-tasks');
    expect(host.textContent).toContain('Running');
    expect(host.textContent).toContain('Waiting for progress');
    expect(host.textContent).not.toContain('Previous scan finished');
    expect(host.querySelector('progress')).toBeNull();
    ui.taskUpdate('scan', {completed: 1, total: 3, message: 'Reading current files'});
    ui.taskBegin('scan', 'Still scanning');
    expect(host.textContent).toContain('Reading current files');
    expect(host.querySelector('progress').value).toBe(1);
});
