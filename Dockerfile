FROM public.ecr.aws/lambda/python:3.11-x86_64

RUN yum install -y git

# Copy requirements.txt
COPY requirements.txt ${LAMBDA_TASK_ROOT}

# Install the specified packages
RUN pip install -r requirements.txt

# Copy function code
COPY lambda_function.py ${LAMBDA_TASK_ROOT}

# Copy application directory
COPY app ${LAMBDA_TASK_ROOT}/app

# Set the CMD to your handler
CMD ["lambda_function.handler"]