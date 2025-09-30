docker build -t lighter-execution-lambda .

aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin 533253328254.dkr.ecr.ap-northeast-2.amazonaws.com

aws ecr create-repository --repository-name lighter-execution --region ap-northeast-2 --image-scanning-configuration scanOnPush=true --image-tag-mutability MUTABLE

docker tag lighter-execution-lambda 533253328254.dkr.ecr.ap-northeast-2.amazonaws.com/lighter-execution:latest

docker push 533253328254.dkr.ecr.ap-northeast-2.amazonaws.com/lighter-execution:latest

