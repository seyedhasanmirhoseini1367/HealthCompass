import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..serializers import ModelPredictionSerializer, AIModelListSerializer

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_predictions(request):
    from apps.ai_insights.models import ModelPrediction
    qs = ModelPrediction.objects.filter(patient=request.user).order_by('-created_at')
    return Response(ModelPredictionSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def prediction_detail_api(request, pk):
    from apps.ai_insights.models import ModelPrediction
    try:
        pred = ModelPrediction.objects.get(pk=pk, patient=request.user)
    except ModelPrediction.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(ModelPredictionSerializer(pred).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_models_list(request):
    from apps.ai_insights.models import AIModel
    qs = AIModel.objects.filter(status='active').order_by('-run_count', '-created_at')
    return Response(AIModelListSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_model_detail(request, slug):
    from apps.ai_insights.models import AIModel
    try:
        model = AIModel.objects.get(slug=slug, status='active')
    except AIModel.DoesNotExist:
        return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    data = AIModelListSerializer(model).data
    data['input_schema'] = model.input_schema
    data['interpretation_guide'] = model.interpretation_guide
    return Response(data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_model_prediction(request, slug):
    from apps.ai_insights.models import AIModel, ModelPrediction
    from apps.ai_insights.services.utils import _sanitize
    from apps.ai_insights.runner import run_model, generate_interpretation
    try:
        model = AIModel.objects.get(slug=slug, status='active')
    except AIModel.DoesNotExist:
        return Response({'error': 'Model not found.'}, status=status.HTTP_404_NOT_FOUND)

    if model.input_type not in ('tabular',):
        return Response(
            {'error': 'This model requires file upload. Please use the website to run it.'},
            status=status.HTTP_400_BAD_REQUEST)

    input_data = {k: request.data.get(k, '') for k in model.input_schema.keys()}
    try:
        result = _sanitize(run_model(model, input_data, None))
        if not result.get('success'):
            raise ValueError(result.get('error', 'Prediction failed'))
        interpretation = generate_interpretation(model, result, input_data)
        pred = ModelPrediction.objects.create(
            model=model, patient=request.user,
            input_data=input_data, result=result,
            risk_score=result.get('risk_score'),
            interpretation=interpretation,
        )
        AIModel.objects.filter(pk=model.pk).update(run_count=model.run_count + 1)
        return Response(ModelPredictionSerializer(pred).data, status=status.HTTP_201_CREATED)
    except Exception as exc:
        return Response({'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def seizure_analysis(request):
    """Proxy EEG parquet/CSV file to hasanai.net seizure comparison API."""
    import requests as http_requests
    from apps.ai_insights.models import AIModel, ModelPrediction

    uploaded_file = request.FILES.get('signal_file')
    if not uploaded_file:
        return Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        resp = http_requests.post(
            'https://hasanai.net/seizure-comparison/predict/',
            files={'signal_file': (uploaded_file.name, uploaded_file.read(), uploaded_file.content_type)},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except http_requests.Timeout:
        return Response({'error': 'Analysis timed out. The file may be too large or the server is busy.'}, status=status.HTTP_504_GATEWAY_TIMEOUT)
    except Exception as exc:
        logger.exception('seizure_analysis proxy error: %s', exc)
        return Response({'error': 'Could not reach the analysis server. Please try again.'}, status=status.HTTP_502_BAD_GATEWAY)

    try:
        User = get_user_model()
        admin_user = User.objects.filter(is_staff=True).first() or request.user
        ai_model, _ = AIModel.objects.get_or_create(
            slug='eeg-seizure-detection',
            defaults={
                'name': 'EEG Seizure Detection',
                'description': 'Ensemble seizure detection via hasanai.net external API.',
                'category': AIModel.Category.NEUROLOGY,
                'input_type': AIModel.InputType.PARQUET,
                'status': AIModel.Status.ACTIVE,
                'data_scientist': admin_user,
            },
        )
        label      = data.get('ensemble_label', '')
        confidence = data.get('ensemble_confidence') or data.get('confidence')
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
        risk_score = None
        if confidence is not None:
            risk_score = confidence if 'seizure' in label.lower() else (1 - confidence)
        pred = ModelPrediction.objects.create(
            model=ai_model,
            patient=request.user,
            input_data={'filename': uploaded_file.name},
            result=data,
            risk_score=risk_score,
        )
        AIModel.objects.filter(pk=ai_model.pk).update(run_count=ai_model.run_count + 1)
        data['prediction_id'] = str(pred.pk)
    except Exception as save_err:
        logger.warning('Could not save seizure prediction: %s', save_err)

    return Response(data)
