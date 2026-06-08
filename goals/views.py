import json
import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST

from .models import HealthGoal, GoalEntry


@login_required
def goal_list(request):
    goals = HealthGoal.objects.filter(
        patient=request.user
    ).prefetch_related('entries').order_by('status', '-created_at')

    goals_data = []
    for goal in goals:
        entries = list(goal.entries.order_by('date'))
        chart = {
            'labels': [str(e.date) for e in entries],
            'values': [e.value     for e in entries],
            'target': goal.target_value,
        }
        goals_data.append({'goal': goal, 'chart_json': json.dumps(chart)})

    return render(request, 'goals/list.html', {
        'goals_data':   goals_data,
        'type_choices': HealthGoal.GoalType.choices,
        'today':        datetime.date.today(),
    })


@login_required
def add_goal(request):
    if request.method == 'POST':
        title       = request.POST.get('title', '').strip()
        goal_type   = request.POST.get('goal_type', 'custom')
        description = request.POST.get('description', '').strip()
        unit        = request.POST.get('unit', '').strip()
        lower       = request.POST.get('lower_is_better') == 'on'
        try:
            target_val  = float(request.POST.get('target_value', ''))
            sv          = request.POST.get('start_value', '').strip()
            start_val   = float(sv) if sv else None
            start_date  = datetime.date.fromisoformat(
                request.POST.get('start_date', str(datetime.date.today())))
            td_str      = request.POST.get('target_date', '').strip()
            target_date = datetime.date.fromisoformat(td_str) if td_str else None
        except (ValueError, TypeError):
            messages.error(request, 'Invalid number or date.')
            return redirect('goals:list')
        if title:
            HealthGoal.objects.create(
                patient=request.user, title=title, goal_type=goal_type,
                description=description, unit=unit, target_value=target_val,
                start_value=start_val, start_date=start_date,
                target_date=target_date, lower_is_better=lower,
            )
            messages.success(request, f'Goal "{title}" created.')
        else:
            messages.error(request, 'Title is required.')
    return redirect('goals:list')


@login_required
def add_entry(request, pk):
    goal = get_object_or_404(HealthGoal, pk=pk, patient=request.user)
    if request.method == 'POST':
        try:
            value      = float(request.POST.get('value', ''))
            date_str   = request.POST.get('date', str(datetime.date.today()))
            entry_date = datetime.date.fromisoformat(date_str)
            note       = request.POST.get('note', '').strip()
            GoalEntry.objects.update_or_create(
                goal=goal, date=entry_date,
                defaults={'value': value, 'note': note},
            )
            # Auto-mark achieved if target reached
            if (not goal.lower_is_better and value >= goal.target_value) or \
               (goal.lower_is_better and value <= goal.target_value):
                goal.status = 'achieved'
                goal.save(update_fields=['status'])
            messages.success(request, f'Logged {value} {goal.unit}')
        except (ValueError, TypeError):
            messages.error(request, 'Invalid value or date.')
    return redirect('goals:list')


@login_required
def update_goal_status(request, pk):
    goal = get_object_or_404(HealthGoal, pk=pk, patient=request.user)
    if request.method == 'POST':
        s = request.POST.get('status')
        if s in dict(HealthGoal.Status.choices):
            goal.status = s
            goal.save(update_fields=['status'])
    return redirect('goals:list')


@login_required
def delete_goal(request, pk):
    goal = get_object_or_404(HealthGoal, pk=pk, patient=request.user)
    if request.method == 'POST':
        title = goal.title
        goal.delete()
        messages.success(request, f'Goal "{title}" deleted.')
    return redirect('goals:list')
