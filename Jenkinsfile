pipeline {
  agent any

  environment {
    AWS_REGION   = 'eu-north-1'
    AWS_ACCOUNT  = '283904064984'
    ECR_REGISTRY = "${AWS_ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    CLUSTER_NAME = 'shivam-hospital-prod'
  }

  stages {

    stage('Checkout') {
      steps {
        checkout scm
      }
    }
stage('SonarQube Analysis') {
  steps {
    script {
      def scannerHome = tool 'sonar-scanner'   // 👈 uses Jenkins config

      withSonarQubeEnv('sonarqube') {
        withCredentials([string(credentialsId: 'sonar', variable: 'SONAR_TOKEN')]) {
          sh """
            ${scannerHome}/bin/sonar-scanner \
              -Dsonar.projectKey=shivam-hospital \
              -Dsonar.sources=services \
              -Dsonar.exclusions=**/node_modules/**,**/.git/**,**/*.log,**/venv/** \
              -Dsonar.login=$SONAR_TOKEN
          """
        }
      }
    }
  }
}
  // 🐳 BUILD
stage('Build Images') {
    steps {
        script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
                sh """
                    docker build \
                        -t ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER} \
                        -f services/${svc}/Dockerfile \
                        services/
                """
            }
        }
    }
} 
stage('Trivy Scan') {
  steps {
    script {
      def services = ['auth','appointment','records','billing','notification','inventory']
      
      // 1. Clean cache once before the loop starts
      sh "trivy clean --scan-cache"

      services.each { svc ->
        echo "Scanning ${svc}..."
        // 2. Use --cache-dir unique to each service to prevent locking collisions
        // 3. Set --exit-code 0 if you want the pipeline to continue even with vulnerabilities
        sh """
          trivy image \
          --cache-dir .trivycache-${svc} \
          --severity HIGH,CRITICAL \
          --exit-code 0 \
          --no-progress \
          ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}
        """
      }
    }
  }
}
    // 📦 PUSH TO ECR
    stage('Push to ECR') {
      steps {
        withCredentials([[
          $class: 'AmazonWebServicesCredentialsBinding',
          credentialsId: 'aws-creds'
        ]]) {
          sh 'aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY'

          script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              sh "docker push ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}"
            }
          }
        }
      }
    }

    // 🚀 DEPLOY
    stage('Deploy to EKS') {
      steps {
        withCredentials([[
          $class: 'AmazonWebServicesCredentialsBinding',
          credentialsId: 'aws-creds'
        ]]) {
          sh 'aws eks update-kubeconfig --name $CLUSTER_NAME --region $AWS_REGION'

          script {
            def services = ['auth','appointment','records','billing','notification','inventory']
            services.each { svc ->
              sh "kubectl set image deployment/${svc}-service ${svc}=${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER} -n backend"
            }
          }
        }
      }
    }
  }

  post {
    success { echo '✅ Deployment successful!' }
    failure { echo '❌ Pipeline failed — check logs' }
  }
}
    // 🐙 GITOPS DEPLOY (Updating your same repo)
    stage('Update Manifests in GitHub') {
      steps {
        script {
          // 1. Tell Jenkins who is making the change
          sh """
            git config user.email "jenkins-bot@hospital.com"
            git config user.name "Jenkins Bot"
          """

          // 2. Use your GitHub Token from Jenkins Credentials
          withCredentials([string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN')]) {
            def services = ['auth','appointment','records','billing','notification','inventory']
            
            services.each { svc ->
              echo "✏️ Updating image for ${svc} in kubernetes/deployments/..."
              
              // 3. This command looks inside the 'deployments' sub-folder 
              // and swaps the old image tag with the new ${BUILD_NUMBER}
              sh "sed -i 's|image: .*/${svc}-service:.*|image: ${ECR_REGISTRY}/shivam-hospital/${svc}-service:${BUILD_NUMBER}|g' kubernetes/deployments/${svc}.yaml"
            }

            // 4. Save and Push the changes back to GitHub
            // [skip ci] tells Jenkins NOT to start a new build after this push
            sh """
              git add kubernetes/deployments/*.yaml
              git commit -m "chore: update images to build ${BUILD_NUMBER} [skip ci]"
              git push https://${GITHUB_TOKEN}@://github.com HEAD:main
            """
          }
        }
      }
    }
